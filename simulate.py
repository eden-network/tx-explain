import os
import json
import asyncio
import requests
import argparse
from google.cloud import bigquery, storage
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

bucket_name = 'tx_explain'

bigquery_client = bigquery.Client()
storage_client = storage.Client()

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
            gas_price, 
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

async def simulate_transaction(tx_hash, block_number, from_address, to_address, gas, gas_price, value, input_data, tx_index, network):
    tenderly_account_slug = os.getenv('TENDERLY_ACCOUNT_SLUG')
    tenderly_project_slug = os.getenv('TENDERLY_PROJECT_SLUG')
    tenderly_access_key = os.getenv('TENDERLY_ACCESS_KEY')

    tx_details = {
        'network_id': NETWORK_CONFIGS[network]['network_id'],
        'block_number': block_number,
        'from': from_address,
        'to': to_address,
        'gas': gas,
        'gas_price': gas_price,
        'value': value,
        'input': input_data,
        'transaction_index': tx_index,
        'simulation_type': 'full',
        'generate_access_list': True,
    }

    print(f'Simulating transaction: {tx_hash}')
    print(tx_details)
    response = requests.post(
        f'https://api.tenderly.co/api/v1/account/{tenderly_account_slug}/project/{tenderly_project_slug}/simulate',
        json=tx_details,
        headers={'X-Access-Key': tenderly_access_key},
    )

    bucket = storage_client.bucket(bucket_name)

    sim_data = response.json()
    print(sim_data)
    if sim_data and 'transaction' in sim_data:
        sim_data['transaction']['hash'] = tx_hash
        sim_data['transaction']['transaction_info']['transaction_id'] = tx_hash
        sim_data['transaction']['transaction_info']['call_trace']['hash'] = tx_hash

        blob = bucket.blob(f'{network}/simulations/full/{block_number}/{tx_hash}.json')
        blob.upload_from_string(json.dumps(sim_data, indent=2))
        print(f'{tx_hash} written successfully to bucket')

        condensed = {}
        call_trace = sim_data['transaction']['transaction_info'].get('call_trace')
        if call_trace:
            condensed['m'] = await condense_calls([call_trace])

        tx_info = sim_data['transaction']['transaction_info']
        if tx_info and 'asset_changes' in tx_info and tx_info['asset_changes']:
            condensed['u'] = await condense_asset_changes(tx_info['asset_changes'])

        if condensed:
            blob = bucket.blob(f'{network}/simulations/condensed/{block_number}/{tx_hash}.json')
            blob.upload_from_string(json.dumps(condensed))
            print(f'{tx_hash} condensed written successfully to bucket')

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
                    tx['gas_price'],
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