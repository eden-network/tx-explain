import os
import json
import asyncio
import requests
import argparse
from google.cloud import bigquery, storage
from datetime import datetime, timedelta
from dotenv import load_dotenv
from web3 import Web3, AsyncWeb3
import decimal
w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider('https://cloudflare-eth.com'))
load_dotenv()

bucket_name = os.getenv('GCS_BUCKET_NAME')

bigquery_client = bigquery.Client()
storage_client = storage.Client()
bucket = storage_client.bucket(bucket_name)

NETWORK_CONFIGS = {
    'ethereum': {
        'table': 'bigquery-public-data.crypto_ethereum.transactions',
        'blocks_table': 'bigquery-public-data.crypto_ethereum.blocks',
        'network_id': '1',
    },
    'arbitrum': {
        'table': 'bigquery-public-data.goog_blockchain_arbitrum_one_us.transactions',
        'blocks_table': 'bigquery-public-data.goog_blockchain_arbitrum_one_us.blocks',
        'network_id': '42161',
    },
    'avalanche': {
        'table': 'bigquery-public-data.goog_blockchain_avalanche_contract_chain_us.transactions',
        'blocks_table': 'bigquery-public-data.goog_blockchain_avalanche_contract_chain_us.blocks',
        'network_id': '43114',
    },
    'optimism': {
        'table': 'bigquery-public-data.goog_blockchain_optimism_mainnet_us.transactions',
        'blocks_table': 'bigquery-public-data.goog_blockchain_optimism_mainnet_us.blocks',
        'network_id': '10',
    },
}

async def sleep(seconds):
    await asyncio.sleep(seconds)

async def condense_calls(calls):
    condensed_calls = []
    for call in calls:
        condensed_call = {
            'c': call.get('contract_name', ''),
            'f': call.get('function_name', ''),
            'a': call.get('from', ''),
            'b': call.get('from_balance', ''),
            'z': call.get('to', ''),
            'x': call.get('input', ''),
            'y': call.get('output', ''),
            'e': call.get('value', ''),
        }

        if 'caller' in call:
            condensed_call['a'] = call['caller'].get('address', '')
            condensed_call['b'] = call['caller'].get('balance', '')

        if 'decoded_input' in call and call['decoded_input']:
            decoded_inputs = []
            for input_data in call['decoded_input']:
                decoded_inputs.append({
                    'n': input_data['soltype'].get('name', ''),
                    't': input_data['soltype'].get('type', ''),
                    'v': input_data.get('value', ''),
                })
            condensed_call['r'] = decoded_inputs

        if 'decoded_output' in call and call['decoded_output']:
            decoded_outputs = []
            for output_data in call['decoded_output']:
                decoded_outputs.append({
                    'n': output_data['soltype'].get('name', ''),
                    't': output_data['soltype'].get('type', ''),
                    'v': output_data.get('value', ''),
                })
            condensed_call['o'] = decoded_outputs

        subcalls = call.get('calls', [])
        if subcalls:
            condensed_call['s'] = await condense_calls(subcalls)

        condensed_calls.append(condensed_call)
    return condensed_calls

async def condense_asset_changes(asset_changes):
    condensed_asset_changes = []
    for asset_change in asset_changes:
        condensed_asset_change = {
            'p': asset_change.get('type', ''),
            'a': asset_change.get('from', ''),
            'z': asset_change.get('to', ''),
            'q': asset_change.get('amount', ''),
            'd': asset_change.get('dollar_value', ''),
        }
        token_info = asset_change.get('token_info', {})
        if token_info:
            condensed_asset_change['g'] = token_info.get('standard', '')
            condensed_asset_change['h'] = token_info.get('type', '')
            condensed_asset_change['i'] = token_info.get('symbol', '')
            condensed_asset_change['j'] = token_info.get('name', '')
            condensed_asset_change['k'] = token_info.get('decimals', '')
            condensed_asset_change['l'] = token_info.get('contract_address', '')
        condensed_asset_changes.append(condensed_asset_change)
    return condensed_asset_changes

async def get_block_ranges_for_date_range(start_day, end_day, network):
    blocks_table = NETWORK_CONFIGS[network]['blocks_table']
    query = f"""
        SELECT 
            MIN(number) AS min_block, 
            MAX(number) AS max_block, 
            STRING(DATE(timestamp)) AS day
        FROM `{blocks_table}`
        WHERE 
            TIMESTAMP_TRUNC(timestamp, DAY) >= TIMESTAMP('{start_day}')
            AND TIMESTAMP_TRUNC(timestamp, DAY) < TIMESTAMP('{end_day}')
        GROUP BY day
        ORDER BY day
    """
    query_job = bigquery_client.query(query)
    print(f"Job {query_job.job_id} started.")
    block_ranges = {}
    for row in query_job:
        block_ranges[row['day']] = {
            'start': row['min_block'],
            'end': row['max_block'],
        }
    return block_ranges

async def query_transactions(start_day, end_day, start_block, end_block, network):
    transactions_table = NETWORK_CONFIGS[network]['table']
    query = f"""
        SELECT 
            `hash`, 
            block_number, 
            from_address, 
            to_address, 
            gas, 
            value, 
            input, 
            transaction_index 
        FROM `{transactions_table}`
        WHERE 
            block_timestamp >= TIMESTAMP('{start_day}')
            AND block_timestamp < TIMESTAMP('{end_day}')
            AND block_number >= {start_block}
            AND block_number <= {end_block}
    """
    query_job = bigquery_client.query(query)
    print(f"Job {query_job.job_id} started.")
    return list(query_job)

async def clean_calltrace(calltrace):
    traces = []
    for call in calltrace:
        trace = {
            'contract_name': call.get('contract_name', ''),
            'function': call.get('function_name', ''),
            'from': call.get('from', ''),
            'from_balance': call.get('from_balance', ''),
            'to': call.get('to', ''),
            'input': call.get('input', ''),
            'output': call.get('output', ''),
            'value': call.get('value', ''),
        }
        if 'error' in call:
            trace['error'] = call.get('error', '')
        if 'caller' in call:
            trace['caller'] = call['caller'].get('address', '')
            trace['caller_balance'] = call['caller'].get('balance', '')
        if 'decoded_input' in call and call['decoded_input']:
            decoded_inputs = []
            for input_data in call['decoded_input']:
                decoded_inputs.append({
                    'name': input_data['soltype'].get('name', ''),
                    'type': input_data['soltype'].get('type', ''),
                    'value': input_data.get('value', ''),
                })
            trace['decoded_input'] = decoded_inputs
        if 'decoded_output' in call and call['decoded_output']:
            decoded_outputs = []
            for output_data in call['decoded_output']:
                decoded_outputs.append({
                    'name': output_data['soltype'].get('name', ''),
                    'type': output_data['soltype'].get('type', ''),
                    'value': output_data.get('value', ''),
                })
            trace['decoded_output'] = decoded_outputs
        subcalls = call.get('calls', [])
        if subcalls:
            trace['calls'] = await clean_calltrace(subcalls)
        traces.append(trace)
    return traces


async def extract_useful_fields(sim_data):
    result = {}
    result['call_trace'] = []
    result['asset_changes'] = []
    call_trace = []
    asset_changes = []
    if 'transaction' in sim_data:
        result['hash'] = sim_data['transaction'].get('hash')
        result['status'] = sim_data['transaction'].get('status', True)
        if result['status'] == False:
            if 'simulation' in sim_data:
                result['error'] = sim_data['simulation'].get('error_message', '')
        if 'transaction_info' in sim_data['transaction']:
            call_trace = sim_data['transaction']['transaction_info'].get('call_trace')
            asset_changes = sim_data['transaction']['transaction_info'].get('asset_changes')

    sim_data = None # Free up memory
    
    if call_trace:
        result['call_trace'] = await clean_calltrace([call_trace])
        
    if asset_changes:
        for asset_change in asset_changes:
            asset_change_summary = {
                'type': asset_change.get('type', ''),
                'from': asset_change.get('from', ''),
                'to': asset_change.get('to', ''),
                'amount': asset_change.get('amount', ''),
                'dollar_value': asset_change.get('dollar_value', ''),
                'token_info': {
                    'standard': asset_change.get('token_info', {}).get('standard', ''),
                    'type': asset_change.get('token_info', {}).get('type', ''),
                    'symbol': asset_change.get('token_info', {}).get('symbol', ''),
                    'name': asset_change.get('token_info', {}).get('name', ''),
                    'decimals': asset_change.get('token_info', {}).get('decimals', ''),
                    'contract_address': asset_change.get('token_info', {}).get('contract_address', ''),
                },
            }
            result['asset_changes'].append(asset_change_summary)
    return result

async def apply_decimals(sim_data):
    result=sim_data
    for call in result['call_trace']:
        if (call["function"]=="approve"):
            #if the call trace is approve, apply decimals directlly on the amount as there is no asset_change object
            token_address=w3.to_checksum_address(call["to"])
            abi='[ { "inputs":[ ], "name":"decimals", "outputs":[ { "internalType":"uint8", "name":"", "type":"uint8" } ], "stateMutability":"view", "type":"function" } ]'
            contract = w3.eth.contract(address=token_address, abi=abi)
            try:
                token_decimals= await contract.functions.decimals().call()
                for decoded_input in call["decoded_input"]:
                    if decoded_input["name"]=="amount":
                        decoded_input["value"]=int(decoded_input["value"])/10**token_decimals
            except:
                print("Web3 call failed")
    return result
async def apply_logs(sim_data):
    result=sim_data
    try:
        transfers=[]
        tokens={}
        tx_hash=sim_data["hash"]
        receipt= await w3.eth.get_transaction_receipt(tx_hash)
        logs=receipt["logs"]
        for log in logs:
            topic0=log["topics"][0].hex()
            if topic0=="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef":
                transfer_from="0x"+w3.to_hex(log["topics"][1])[26:]
                transfer_to="0x"+w3.to_hex(log["topics"][2])[26:]
                transfer_amount=w3.to_int(log["data"])
                token_address=log["address"]
                if token_address not in tokens:
                    token_address_contract=w3.to_checksum_address(token_address)
                    abi='[{"constant": true,"inputs": [],"name": "name","outputs": [{"name": "","type": "string"}],"payable": false,"stateMutability": "view","type": "function"},{"constant": false,"inputs": [{"name": "_spender","type": "address"},{"name": "_value","type": "uint256"}],"name": "approve","outputs": [{"name": "","type": "bool"}],"payable": false,"stateMutability": "nonpayable","type": "function"},{"constant": true,"inputs": [],"name": "totalSupply","outputs": [{"name": "","type": "uint256"}],"payable": false,"stateMutability": "view","type": "function"},{"constant": false,"inputs": [{"name": "_from","type": "address"},{"name": "_to","type": "address"},{"name": "_value","type": "uint256"}],"name": "transferFrom","outputs": [{"name": "","type": "bool"}],"payable": false,"stateMutability": "nonpayable","type": "function"},{"constant": true,"inputs": [],"name": "decimals","outputs": [{"name": "","type": "uint8"}],"payable": false,"stateMutability": "view","type": "function"},{"constant": true,"inputs": [{"name": "_owner","type": "address"}],"name": "balanceOf","outputs": [{"name": "balance","type": "uint256"}],"payable": false,"stateMutability": "view","type": "function"},{"constant": true,"inputs": [],"name": "symbol","outputs": [{"name": "","type": "string"}],"payable": false,"stateMutability": "view","type": "function"},{"constant": false,"inputs": [{"name": "_to","type": "address"},{"name": "_value","type": "uint256"}],"name": "transfer","outputs": [{"name": "","type": "bool"}],"payable": false,"stateMutability": "nonpayable","type": "function"},{"constant": true,"inputs": [{"name": "_owner","type": "address"},{"name": "_spender","type": "address"}],"name": "allowance","outputs": [{"name": "","type": "uint256"}],"payable": false,"stateMutability": "view","type": "function"},{"payable": true,"stateMutability": "payable","type": "fallback"},{"anonymous": false,"inputs": [{"indexed": true,"name": "owner","type": "address"},{"indexed": true,"name": "spender","type": "address"},{"indexed": false,"name": "value","type": "uint256"}],"name": "Approval","type": "event"},{"anonymous": false,"inputs": [{"indexed": true,"name": "from","type": "address"},{"indexed": true,"name": "to","type": "address"},{"indexed": false,"name": "value","type": "uint256"}],"name": "Transfer","type": "event"}]'
                    contract = w3.eth.contract(address=token_address_contract, abi=abi)
                    token_decimals= await contract.functions.decimals().call()
                    token_name= await contract.functions.name().call()
                    token_symbol= await contract.functions.symbol().call()
                    token_obj={
                        "name": token_name,
                        "symbol" : token_symbol,
                        "decimals" : int(token_decimals)
                    }
                    tokens[token_address]=token_obj
                transfer_obj={
                    "token_address": token_address,
                    "from" : transfer_from,
                    "to" : transfer_to,
                    "amount" : decimal.Decimal(transfer_amount)/decimal.Decimal(10**tokens[token_address]["decimals"]),
                    "token_name" : tokens[token_address]["name"],
                    "token_symbol" : tokens[token_address]["symbol"],
                    "token_decimals" : tokens[token_address]["decimals"]
                }
                transfers.append(transfer_obj)

        for asset_change in result["asset_changes"]:
            if asset_change["amount"] is None:
                token_address=asset_change["token_info"]["contract_address"]
                transfer_from=asset_change["from"]
                transfer_to=asset_change["to"]
                for transfer in transfers:
                    if (transfer["from"]==transfer_from and transfer["to"]==transfer_to and transfer["token_address"].lower()==token_address.lower()):
                        asset_change["amount"]=str(transfer["amount"])
                        asset_change["token_info"]["symbol"]=transfer["token_symbol"]
                        asset_change["token_info"]["name"]=transfer["token_name"]
                        asset_change["token_info"]["decimals"]=transfer["token_decimals"]    
                        asset_change["token_info"]["type"]="Fungible"
    except :
        print ("Error in web3 call - logs")
    return result
async def get_cached_simulation(tx_hash, network):
    blob = bucket.blob(f'{network}/transactions/simulations/trimmed/{tx_hash}.json')
    if blob.exists():
        return json.loads(blob.download_as_string())
    return None

async def simulate_transaction(tx_hash, block_number, from_address, to_address, gas, value, input_data, tx_index, network):
    tenderly_account_slug = os.getenv('TENDERLY_ACCOUNT_SLUG')
    tenderly_project_slug = os.getenv('TENDERLY_PROJECT_SLUG')
    tenderly_access_key = os.getenv('TENDERLY_ACCESS_KEY')

    tx_details = {
        'network_id': NETWORK_CONFIGS[network]['network_id'],
        'block_number': block_number,
        'from': from_address,
        'to': to_address,
        'gas': gas,
        'value': value,
        'input': input_data,
        'transaction_index': tx_index,
        'simulation_type': 'full',
        'generate_access_list': True,
    }

    print(f'Simulating transaction: {tx_hash}')
    response = requests.post(
        f'https://api.tenderly.co/api/v1/account/{tenderly_account_slug}/project/{tenderly_project_slug}/simulate',
        json=tx_details,
        headers={'X-Access-Key': tenderly_access_key},
    )

    sim_data = response.json()
    if sim_data and 'transaction' in sim_data:
        sim_data['transaction']['hash'] = tx_hash
        if 'transaction_info' in sim_data['transaction']:
            sim_data['transaction']['transaction_info']['transaction_id'] = tx_hash
            if 'call_trace' in sim_data['transaction']['transaction_info']:
                sim_data['transaction']['transaction_info']['call_trace']['hash'] = tx_hash
        try:
            blob = bucket.blob(f'{network}/transactions/simulations/full/{tx_hash}.json')
            blob.upload_from_string(json.dumps(sim_data))
            print(f'{"\n"} full simulation written successfully to bucket')
        except Exception as e:
            print(f'Error uploading full simulation for {tx_hash}: {str(e)}')
        f=open("static/log.txt","a")
        f.write("\n")
        f.write(str(tx_hash))
        f.write("\n")
        f.write("Exctracting useful fields \n")
        trimmed_initial = await extract_useful_fields(sim_data)
        f.write("Applying decimals \n")
        trimmed_decimals = await apply_decimals(trimmed_initial)
        f.write("Applying logs \n")
        trimmed= await apply_logs(trimmed_decimals)
        f.write("Done \n")
        try:
            blob = bucket.blob(f'{network}/transactions/simulations/trimmed/{tx_hash}.json')
            blob.upload_from_string(json.dumps(trimmed))
            print(f'{tx_hash} trimmed simulation written successfully to bucket')
        except Exception as e:
            print(f'Error uploading trimmed simulation for {tx_hash}: {str(e)}')
        return trimmed
    return None
async def main(start_day, end_day, network):
    block_ranges = await get_block_ranges_for_date_range(start_day, end_day, network)

    current_day = datetime.strptime(start_day, '%Y-%m-%d')
    end_date = datetime.strptime(end_day, '%Y-%m-%d')

    while current_day <= end_date:
        day = current_day.strftime('%Y-%m-%d')
        next_day = (current_day + timedelta(days=1)).strftime('%Y-%m-%d')

        day_block_range = block_ranges[day]
        block_number = day_block_range['start']
        while block_number <= day_block_range['end']:
            print(f"{day}: Querying transactions for block range {block_number} - {block_number + 1000}")
            transactions = await query_transactions(day, next_day, block_number, block_number + 1000, network)
            for tx in transactions:
                await simulate_transaction(
                    tx['hash'],
                    tx['block_number'],
                    tx['from_address'],
                    tx['to_address'],
                    tx['gas'],
                    str(tx['value']),
                    tx['input'],
                    tx['transaction_index'],
                    network
                )
                await sleep(0.2)
            await sleep(1)
            block_number += 1000

        current_day += timedelta(days=1)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Blockchain Transaction Simulator')
    parser.add_argument('-s', '--start', type=str, default=(datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d'),
                        help='Start day for transaction simulation (default: yesterday)')
    parser.add_argument('-e', '--end', type=str, default=datetime.today().strftime('%Y-%m-%d'),
                        help='End day for transaction simulation (default: today)')
    parser.add_argument('-n', '--network', type=str, default='ethereum', choices=['ethereum', 'arbitrum', 'avalanche', 'optimism'],
                        help='Blockchain network to simulate transactions for (default: ethereum)')
    args = parser.parse_args()

    start_day = args.start
    end_day = args.end
    network = args.network

    asyncio.run(main(start_day, end_day, network))
