import asyncio
import aiohttp
import requests
import os
from web3 import AsyncWeb3
from dotenv import load_dotenv
import json
from anthropic import AsyncAnthropic
from datetime import datetime
from collections import defaultdict
from google.cloud import storage
import re

from label import addresses_to_string, async_query_flipside, to_json
from simulate import simulate_transaction
from explain import explain_transaction

load_dotenv()

tenderly_account_slug = os.getenv('TENDERLY_ACCOUNT_SLUG')
tenderly_project_slug = os.getenv('TENDERLY_PROJECT_SLUG')
tenderly_access_key = os.getenv('TENDERLY_ACCESS_KEY')
debank_api_key = os.getenv('DEBANK_API_KEY')

BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')
storage_client = storage.Client()
bucket = storage_client.bucket(BUCKET_NAME)

network_endpoints = {
            '1': (os.getenv('ETH_RPC_ENDPOINT'), 'ethereum'),
            '42161': (os.getenv('ARB_RPC_ENDPOINT'), 'arbitrum'),
            '10': (os.getenv('OP_RPC_ENDPOINT'), 'optimism'),
            '43114': (os.getenv('AVAX_RPC_ENDPOINT'), 'avalanche'),
            '8453': (os.getenv('BASE_RPC_ENDPOINT'), 'base'),
            '81467': (os.getenv('BLAST_RPC_ENDPOINT'), 'blast'),
            '5000': (os.getenv('MANTLE_RPC_ENDPOINT'), 'mantle')
        }

# Translation dictionary
chain_id_translation = {
    'ethereum': 'eth',
    'arbitrum': 'arb',
    'optimism': 'op',
    'avalanche': 'avax',
    'base': None,
    'blast': None,
    'mantle': None
}

# Decodes timestamps
def decode_timestamp(timestamp):
    try:
        date_time = datetime.utcfromtimestamp(timestamp)
        decoded_timestamp = date_time.strftime('%Y-%m-%d %H:%M:%S')
        return decoded_timestamp
    except Exception as e:
        print("Error at decode_timestamp: ", e)

# Extracts addresses from a string
async def extract_addresses(history):
    history_str = json.dumps(history)
    pattern = r"0x[a-fA-F0-9]{40}"
    matches = re.findall(pattern, history_str)
    unique_matches = list(set(matches))
    
    return unique_matches

# Returns account positions from Debank
async def get_debank_positions(address, network_id, debank_api_key):
    try:
        chain_id_full = network_endpoints.get(network_id)[1]
        chain_id = chain_id_translation.get(chain_id_full)
        if chain_id is None:
            raise ValueError(f"The chain {chain_id_full} is not supported on Debank.")

        url = f"https://pro-openapi.debank.com/v1/user/complex_protocol_list"
        headers = {
            "accept": "application/json",
            "AccessKey": debank_api_key
        }
        params = {
            "id": address,
            "chain_id": chain_id
        }
    
        response = requests.get(url, headers=headers, params=params)
    
        if response.status_code == 200:
            return response.json()
        else:
            response.raise_for_status()
    except Exception as e:
        print("Error at get_debank_positions: ", e)

# Returns account transaction history from Debank
async def get_debank_history(address, network_id, debank_api_key):
    try:
        chain_id_full = network_endpoints.get(network_id)[1]
        chain_id = chain_id_translation.get(chain_id_full)
        if chain_id is None:
            raise ValueError(f"The chain {chain_id_full} is not supported on Debank.")

        url = "https://pro-openapi.debank.com/v1/user/history_list"
        headers = {
            "accept": "application/json",
            "AccessKey": debank_api_key
        }
        params = {
            "id": address,
            "chain_id": chain_id,
            "page_count": 20
        }
    
        response = requests.get(url, headers=headers, params=params)
    
        if response.status_code == 200:
            return response.json()
        else:
            response.raise_for_status()
    except Exception as e:
        print("Error at get_debank_history: ", e)

# Returns web3 tx details
async def get_tx_details(tx_hash, network_id):
    try:
        # Retrieve the base URL from the network_endpoints dictionary
        base_url = network_endpoints.get(network_id)[0]

        if not base_url:
            raise ValueError(f"No endpoint found for network_id {network_id}")

        headers = {'Content-Type': 'application/json'}
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getTransactionByHash",
            "params": [tx_hash],
            "id": 1
        }
        response = requests.post(base_url, json=payload, headers=headers)

        if response.status_code != 200:
            raise ValueError(f"Error response from server: {response.status_code} {response.text}")

        result = response.json().get('result')
        return result

    except Exception as e:
        print("Error at get_tx_details: ", e)
        return None

# Simulates and explains a transaction
async def fetch_and_explain(transaction, network_id, transaction_prompt, client):
    tx_hash = transaction['hash']
    network = network_endpoints.get(network_id)[1]
    try:
        blob = bucket.blob(f'{network}/transactions/explanations/{tx_hash}.json')
        if blob.exists():
            cached_data = json.loads(blob.download_as_string())
            return {
                'tx_hash': tx_hash,
                'explanation': cached_data
            }
        else:
            simulation_response = await simulate_transaction(tx_hash, 
                                                             int(transaction['blockNumber'], 16), 
                                                             transaction['from'], 
                                                             transaction['to'], 
                                                             int(transaction['gas'], 16), 
                                                             int(transaction['value'], 16), 
                                                             transaction['input'], 
                                                             int(transaction['transactionIndex'], 16), 
                                                             network,
                                                             store_result=False)

            explanation_generator = explain_transaction(client = client, 
                                                        payload = simulation_response, 
                                                        network = network, 
                                                        system_prompt = transaction_prompt,
                                                        model="claude-3-haiku-20240307", 
                                                        max_tokens=2000, 
                                                        temperature=0, 
                                                        store_result=False)
            explanation = ""
            async for word in explanation_generator:
                explanation += word

            return {
                'tx_hash': tx_hash,
                'explanation': explanation
            }
    except Exception as e:
        print("Error at fetch_and_explain: ", e)

# Creates account overview dictionary, makes the data more readable to the model
async def get_account_stats(history):
    try:
        transactions = history['history_list']
        if not transactions:
            return history

        total_transactions = len(transactions)
        total_gas_fees_eth = sum(tx["tx"]["eth_gas_fee"] for tx in transactions if tx is not None and "tx" in tx and tx["tx"] is not None and "eth_gas_fee" in tx["tx"])
        total_gas_fees_usd = sum(tx["tx"]["usd_gas_fee"] for tx in transactions if tx is not None and "tx" in tx and tx["tx"] is not None and "usd_gas_fee" in tx["tx"])

        tokens_involved = set()
        address_interaction_pattern = {"sent_to": defaultdict(int), "received_from": defaultdict(int)}
        eoa_initiated_transactions = defaultdict(int)
        times = []

        for tx in transactions:
            time_at = datetime.strptime(tx["time_at"], "%Y-%m-%d %H:%M:%S")
            times.append(time_at)

            if "tx" in tx and "from_eoa" in tx["tx"]:
                from_address = tx["tx"]["from_eoa"]
                eoa_initiated_transactions[from_address] += 1

            if "sends" in tx and tx["sends"]:
                for send in tx["sends"]:
                    tokens_involved.add(send["token_id"])
                    address_interaction_pattern["sent_to"][send["to_addr"]] += 1

            if "receives" in tx and tx["receives"]:
                for receive in tx["receives"]:
                    tokens_involved.add(receive["token_id"])
                    address_interaction_pattern["received_from"][receive["from_addr"]] += 1

        time_range = (min(times), max(times))
        duration_minutes = (time_range[1] - time_range[0]).total_seconds() / 60

        history["user_account_overview"] = {
            "total_transactions": total_transactions,
            "time_range": (time_range[0].strftime("%Y-%m-%d %H:%M:%S"), time_range[1].strftime("%Y-%m-%d %H:%M:%S")),
            "total_gas_fees_eth": total_gas_fees_eth,
            "total_gas_fees_usd": total_gas_fees_usd,
            "tokens_involved": list(tokens_involved),
            "address_interaction_pattern": {
                "initiated_by_eoa": dict(eoa_initiated_transactions),
                "sent_to": dict(address_interaction_pattern["sent_to"]),
                "received_from": dict(address_interaction_pattern["received_from"]),
            },
        }

        return history

    except Exception as e:
        print("Error at get_accout_stats: ", e)
        return None

# Removes unecessary data from transaction history data
async def clean_history_data(original_data):
    transformed_data = {
        "cate_dict": original_data.get("cate_dict", {}),
        "cex_dict": original_data.get("cex_dict", {}),
        "history_list": [],
        "project_dict": original_data.get("project_dict", {}),
        "token_dict": {},
    }
    
    # Process transaction history
    for history_item in original_data.get("history_list", []):
        transformed_history_item = {
            "cate_id": history_item.get("cate_id", ""),
            "cex_id": history_item.get("cex_id", ""),
            "id": history_item.get("id", ""),
            "is_scam": history_item.get("is_scam", False),
            "other_addr": history_item.get("other_addr", ""),
            "project_id": history_item.get("project_id", ""),
            "receives": history_item.get("receives", []),
            "sends": history_item.get("sends", []),
            "time_at": history_item.get("time_at", ""),
            "token_approve": history_item.get("token_approve", ""),
            "tx": {
                "from_addr": history_item["tx"].get("from_addr", ""),
                "message": history_item["tx"].get("message", ""),
                "name": history_item["tx"].get("name", ""),
                "params": history_item["tx"].get("params", []),
                "status": history_item["tx"].get("status", 0),
                "to_addr": history_item["tx"].get("to_addr", ""),
                "value": history_item["tx"].get("value", 0.0)
            }
        }
        transformed_data["history_list"].append(transformed_history_item)
    
    # Clean token_dict entries
    for token_id, token_info in original_data.get("token_dict", {}).items():
        cleaned_token_info = {
            "decimals": token_info.get("decimals", 0),
            "id": token_info.get("id", ""),
            "is_core": token_info.get("is_core", False),
            "is_scam": token_info.get("is_scam", False),
            "is_suspicious": token_info.get("is_suspicious", False),
            "is_verified": token_info.get("is_verified", False),
            "is_wallet": token_info.get("is_wallet", False),
            "name": token_info.get("name", ""),
            "price": token_info.get("price", 0.0),
            "price_24h_change": token_info.get("price_24h_change", 0.0)
        }
        transformed_data["token_dict"][token_id] = cleaned_token_info
    
    return transformed_data

# Removes unecessary data from positions data
async def clean_portfolio_data(original_data):
    cleaned_data = []
    
    for project in original_data:
        cleaned_project = {
            "id": project.get("id", ""),
            "name": project.get("name", ""),
            "tvl": project.get("tvl", 0.0),
            "portfolio_item_list": []
        }
        
        for item in project.get("portfolio_item_list", []):
            cleaned_item = {
                "stats": {
                    "asset_usd_value": item["stats"].get("asset_usd_value", 0.0),
                    "debt_usd_value": item["stats"].get("debt_usd_value", 0.0),
                    "net_usd_value": item["stats"].get("net_usd_value", 0.0)
                },
                "asset_dict": item.get("asset_dict", {}),
                "asset_token_list": [{
                    "id": token.get("id", ""),
                    "chain": token.get("chain", ""),
                    "name": token.get("name", ""),
                    "decimals": token.get("decimals", 0),
                    "price": token.get("price", 0.0),
                    "amount": token.get("amount", 0.0)
                } for token in item.get("asset_token_list", [])],
                "name": item.get("name", ""),
                "detail_types": item.get("detail_types", []),
                "detail": {
                    "supply_token_list": [{
                        "id": supply_token.get("id", ""),
                        "name": supply_token.get("name", ""),
                        "decimals": supply_token.get("decimals", 0),
                        "price": supply_token.get("price", 0.0),
                        "amount": supply_token.get("amount", 0.0)
                    } for supply_token in item.get("detail", {}).get("supply_token_list", [])],
                    "unlock_at": item.get("detail", {}).get("unlock_at", None)
                },
                "pool": {
                    "id": item.get("pool", {}).get("id", ""),
                    "chain": item.get("pool", {}).get("chain", ""),
                    "project_id": item.get("pool", {}).get("project_id", ""),
                    "adapter_id": item.get("pool", {}).get("adapter_id", ""),
                    "controller": item.get("pool", {}).get("controller", ""),
                    "index": item.get("pool", {}).get("index", None),
                    "time_at": item.get("pool", {}).get("time_at", 0)
                }
            }
            cleaned_project["portfolio_item_list"].append(cleaned_item)
        
        cleaned_data.append(cleaned_project)
    
    return cleaned_data

async def explainAccount(address, network_id, summarize_prompt, transaction_prompt):
    try:
        # Load the prompt
        summarize_prompt = summarize_prompt.format(address=address)
        
        # Get debank data
        positions = await get_debank_positions(address, network_id, debank_api_key) 
        history = await get_debank_history(address, network_id, debank_api_key) 

        # Clean data
        positions = await clean_portfolio_data(positions)
        history = await clean_history_data(history)
        history["history_list"] = [{**record, "time_at": decode_timestamp(record["time_at"])}for record in history["history_list"]]

        # Extract tx_hashes
        transactions = [x['id'] for x in history['history_list']]

        # Get web3 details for transactions
        fetching_tx_details = [get_tx_details(tx_hash, network_id) for tx_hash in transactions]
        tx_details = await asyncio.gather(*fetching_tx_details)

        # Adding tx details to the transaction history list
        tx_details_dict = {tx['hash']: tx for tx in tx_details}
        for item in history['history_list']:
            tx_id = item['id']
            if tx_id in tx_details_dict:
                from_address = tx_details_dict[tx_id]['from']
                block_number = int(tx_details_dict[tx_id]['blockNumber'],16)
                transaction_index = int(tx_details_dict[tx_id]['transactionIndex'],16)
                transaction_type = int(tx_details_dict[tx_id]['type'],16)

                item['tx']['from_eoa'] = from_address # Needed to add this because debank doesn't return the actual from_address of the transaction
                item['tx']['block_number'] = block_number
                item['tx']['transaction_index'] = transaction_index
                item['tx']['transaction_type'] = transaction_type

        # Limiting the transaction number to be simulated and explained
        tx_details = tx_details[:5]
        
        # Extracting addresses to be labelled 
        addresses_str = addresses_to_string(await extract_addresses(history))

        async with aiohttp.ClientSession() as session:
            anthropic_client = AsyncAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

            tasks = [fetch_and_explain(transaction, network_id, transaction_prompt, anthropic_client) for transaction in tx_details]
            flipside_task = async_query_flipside(addresses_str, network_endpoints.get(network_id)[1])

            explanations, address_labels = await asyncio.gather(
                asyncio.gather(*tasks), 
                flipside_task
            )

            explanation_dict = {explanation['tx_hash']: explanation for explanation in explanations}
            for record in history['history_list']:
                tx_id = record['id']
                if tx_id in explanation_dict:
                    record['tx_explanation'] = explanation_dict[tx_id]['explanation']

            history = await get_account_stats(history)
            
            account_payload = {
                "user": address,
                "network": network_endpoints.get(network_id)[1],
                "positions": positions,
                "transaction_history": history,
                "address_labels": to_json(address_labels)
            }
            print(json.dumps(account_payload, indent = 4))

            message = f"""Here is the data for address {address} on {network_endpoints.get(network_id)[1]} blockchain. \n    
                                  I want you to provide a high-level summary of the address based on the data you will be provided. \n
                                  The summary should be unstructured and flash out the important bits about the account. \n
                                  The purpose of the summary is to understand what the address is all about without being too technical. \n
                                  Account data: \n
                                  {json.dumps(account_payload, indent = 4)}       
                            """
            explanation_generator = explain_transaction(anthropic_client, 
                                                            message, 
                                                            network=None,
                                                            system_prompt=summarize_prompt, 
                                                            model="claude-3-haiku-20240307", 
                                                            max_tokens=2000, 
                                                            temperature=0, 
                                                            store_result=False
                                                            )
            summarized_account = ""
            async for word in explanation_generator:
                summarized_account += word
        
            return summarized_account
    except Exception as e:
        print("Error at explainAccount: ", e)