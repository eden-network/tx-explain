import os
import json
import asyncio
import aiohttp
import argparse
import logging
from google.cloud import bigquery, storage
from datetime import datetime, timedelta
from dotenv import load_dotenv
from web3 import AsyncWeb3
import decimal
from flipside import Flipside
from label import add_labels

w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider('https://cloudflare-eth.com'))
load_dotenv()

logging.getLogger().setLevel(logging.INFO)
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
    'base': {
        'table': 'none',
        'blocks_table': 'none',
        'network_id': '8453',
    },
    'blast': {
        'table': 'none',
        'blocks_table': 'none',
        'network_id': '81467',
    },
    'mantle': {
        'table': 'none',
        'blocks_table': 'none',
        'network_id': '5000',
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


async def clean_calltrace(calltrace, depth=0):
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

        if trace["function"]=="approve":
            token_address=w3.to_checksum_address(call["to"])
            abi='[ { "inputs":[ ], "name":"decimals", "outputs":[ { "internalType":"uint8", "name":"", "type":"uint8" } ], "stateMutability":"view", "type":"function" } ]'
            contract = w3.eth.contract(address=token_address, abi=abi)
            try:
                trace['decimals'] = await contract.functions.decimals().call()                
            except:
                logging.info("Web3 call failed: " + str(token_address))
        if 'error' in call:
            trace['error'] = call.get('error', '')
        if 'caller' in call:
            trace['caller'] = call['caller'].get('address', '')
            trace['caller_balance'] = call['caller'].get('balance', '')
        if 'decoded_input' in call:
            decoded_inputs = []
            for input_data in call['decoded_input']:
                input_name = input_data['soltype'].get('name', ''),
                input_type = input_data['soltype'].get('type', ''),
                input_value = input_data.get('value', '')
                decimals = trace.get('decimals')
                if input_value and decimals and int(decimals) > 0 and (input_name=="amount" or input_name=="_value"):
                    input_value = str(int(input_value) / 10**int(decimals))
                decoded_inputs.append({
                    'name': input_name,
                    'type': input_type,
                    'value': input_value,
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
        if subcalls and depth < 2:
            trace['calls'] = await clean_calltrace(subcalls, depth + 1)
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


async def fetch_tenderly_simulation(tx_details, tenderly_account_slug, tenderly_project_slug, tenderly_access_key, session):
    async with session.post(
        f'https://api.tenderly.co/api/v1/account/{tenderly_account_slug}/project/{tenderly_project_slug}/simulate',
        json=tx_details,
        headers={'X-Access-Key': tenderly_access_key}
    ) as response:
        return await response.json()

async def simulate_pending_transaction_tenderly(tx_hash, block_number, from_address, to_address, gas, value, input_data, tx_index, network, store_result=True):
    tenderly_account_slug = os.getenv('TENDERLY_ACCOUNT_SLUG')
    tenderly_project_slug = os.getenv('TENDERLY_PROJECT_SLUG')
    tenderly_access_key = os.getenv('TENDERLY_ACCESS_KEY')
    flipside_api_key = os.getenv('FLIPSIDE_API_KEY')
    flipside_endpoint_url = os.getenv('FLIPSIDE_ENDPOINT_URL')
    flipside = Flipside(flipside_api_key, flipside_endpoint_url)

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
    async with aiohttp.ClientSession() as session:
        logging.info(f'Simulating transaction: {tx_hash}')

        sim_data = await fetch_tenderly_simulation(tx_details, tenderly_account_slug, tenderly_project_slug, tenderly_access_key, session)
        print(sim_data)
        if "error" in sim_data:
            return sim_data
        if sim_data and 'transaction' in sim_data:
            sim_data['transaction']['hash'] = tx_hash
            if 'transaction_info' in sim_data['transaction']:
                sim_data['transaction']['transaction_info']['transaction_id'] = tx_hash
                if 'call_trace' in sim_data['transaction']['transaction_info']:
                    sim_data['transaction']['transaction_info']['call_trace']['hash'] = tx_hash
            
            if store_result:
                print("Storing the full simulation to bucket...")
                try:
                    blob = bucket.blob(f'{network}/transactions/simulations/full/{tx_hash}.json')
                    blob.upload_from_string(json.dumps(sim_data))
                    logging.info(f'{tx_hash} full simulation written successfully to bucket')
                except Exception as e:
                    logging.error(f'Error uploading full simulation for {tx_hash}: {str(e)}')

            trimmed = await extract_useful_fields(sim_data)
            
            if store_result:
                print("Storing the trimmed simulation to bucket...")
                try:
                    blob = bucket.blob(f'{network}/transactions/simulations/trimmed/{tx_hash}.json')
                    blob.upload_from_string(json.dumps(trimmed))
                    logging.info(f'{tx_hash} trimmed simulation written successfully to bucket')
                except Exception as e:
                    logging.error(f'Error uploading trimmed simulation for {tx_hash}: {str(e)}')
            return trimmed
    return None
