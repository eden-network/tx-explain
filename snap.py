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
from anthropic import AsyncAnthropic
from simulate import sleep, condense_calls, condense_asset_changes, clean_calltrace, extract_useful_fields, fetch_tenderly_simulation


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


async def simulate_pending_transaction_tenderly_snap(tx_hash, block_number, from_address, to_address, gas, value, input_data, network):
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
        'simulation_type': 'full',
        'generate_access_list': True,
    }
    async with aiohttp.ClientSession() as session:
        logging.info(f'Simulating transaction: {tx_hash}')

        sim_data = await fetch_tenderly_simulation(tx_details, tenderly_account_slug, tenderly_project_slug, tenderly_access_key, session)
        print (sim_data)
        if "error" in sim_data:
            return (sim_data)
        if sim_data and 'transaction' in sim_data:
            sim_data['transaction']['hash'] = tx_hash
            if 'transaction_info' in sim_data['transaction']:
                sim_data['transaction']['transaction_info']['transaction_id'] = tx_hash
                if 'call_trace' in sim_data['transaction']['transaction_info']:
                    sim_data['transaction']['transaction_info']['call_trace']['hash'] = tx_hash
            trimmed = await extract_useful_fields(sim_data)

            return trimmed
    return None
