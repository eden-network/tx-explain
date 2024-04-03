import os
import json
import requests
from simulate import simulate_transaction
from explain import explain_transaction
from flask import Flask, request, jsonify
from anthropic import AsyncAnthropic
from google.cloud import storage
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
storage_client = storage.Client()
GCS_BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')
GCS_BUCKET = storage_client.bucket(GCS_BUCKET_NAME)
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
ANTHROPIC_CLIENT = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
DEFAULT_MODEL = 'claude-3-opus-20240229'
DEFAULT_MAX_TOKENS = 2000
DEFAULT_TEMPERATURE = 0
DEFAULT_SYSTEM_PROMPT = None
with open('system_prompt.txt', 'r') as file:
    DEFAULT_SYSTEM_PROMPT = file.read()

async def simulate_txs(transactions, network):
    result = []
    for transaction in transactions:
        condensed_simulation = await simulate_transaction(transaction.hash, transaction.block_number, transaction.from_address, transaction.to_address, transaction.gas, transaction.gas_price, transaction.value, transaction.input, transaction.transaction_index, network)
        result.append(condensed_simulation)
    return result

async def explain_txs(transactions, system_prompt, model, max_tokens, temperature):
    result = []
    for transaction in transactions:
        explanation = await explain_transaction(ANTHROPIC_CLIENT, transaction, system_prompt, model, max_tokens, temperature)
        result.append(explanation)
    return result

async def simulate_and_explain_txs(transactions, network, system_prompt, model, max_tokens, temperature):
    result = []
    for transaction in transactions:
        condensed_simulation = await simulate_transaction(transaction.hash, transaction.block_number, transaction.from_address, transaction.to_address, transaction.gas, transaction.gas_price, transaction.value, transaction.input, transaction.transaction_index, network)
        explanation = await explain_transaction(ANTHROPIC_CLIENT, condensed_simulation, system_prompt, model, max_tokens, temperature)
        result.append(explanation)
    return result

@app.route('/v1/transaction/simulate', methods=['POST'])
async def simulate_transactions():
    data = request.json
    transactions = data['transactions']
    network = data.get('network', 'ethereum')
    result = await simulate_txs(transactions, network)
    return jsonify({'result': result})

@app.route('/v1/transaction/explain', methods=['POST'])
async def explain_transactions():
    data = request.json
    transactions = data['transactions']
    system_prompt = DEFAULT_SYSTEM_PROMPT
    model = DEFAULT_MODEL
    max_tokens = DEFAULT_MAX_TOKENS
    temperature = DEFAULT_TEMPERATURE
    if data['system']:
        system_prompt = data['system']
    if data['model']:
        model = data['model']
    if data['max_tokens']:
        max_tokens = data['max_tokens']
    if data['temperature']:
        temperature = data['temperature']
    result = await explain_txs(transactions, system_prompt, model, max_tokens, temperature)
    return jsonify({'result': result})

@app.route('/v1/transaction/simulate_and_explain', methods=['POST'])
async def simulate_and_explain_transactions():
    data = request.json
    transactions = data['transactions']
    network = data.get('network', 'ethereum')
    system_prompt = DEFAULT_SYSTEM_PROMPT
    model = DEFAULT_MODEL
    max_tokens = DEFAULT_MAX_TOKENS
    temperature = DEFAULT_TEMPERATURE
    if data['system']:
        system_prompt = data['system']
    if data['model']:
        model = data['model']
    if data['max_tokens']:
        max_tokens = data['max_tokens']
    if data['temperature']:
        temperature = data['temperature']
    result = []
    for transaction in transactions:
        condensed_simulation = await simulate_transaction(transaction.hash, transaction.block_number, transaction.from_address, transaction.to_address, transaction.gas, transaction.gas_price, transaction.value, transaction.input, transaction.transaction_index, network)
        explanation = await explain_transaction(ANTHROPIC_CLIENT, condensed_simulation, system_prompt, model, max_tokens, temperature)
        result.append(explanation)
    return jsonify({'result': result})

@app.route('/v1/transaction', methods=['POST'])
async def get_transaction():
    data = request.json
    tx_hash = data.get('tx_hash')
    network_id = data.get('network_id')
    system_prompt = DEFAULT_SYSTEM_PROMPT
    model = DEFAULT_MODEL
    max_tokens = DEFAULT_MAX_TOKENS
    temperature = DEFAULT_TEMPERATURE
    if data['system']:
        system_prompt = data['system']
    if data['model']:
        model = data['model']
    if data['max_tokens']:
        max_tokens = data['max_tokens']
    if data['temperature']:
        temperature = data['temperature']

    if not network_id:
        return jsonify({'error': 'Missing network ID'}), 400
    
    if not tx_hash:
        return jsonify({'error': 'Missing transaction hash'}), 400
    
    # Map network IDs to RPC API endpoints and network names
    network_endpoints = {
        '1': (os.getenv('ETH_RPC_ENDPOINT'), 'ethereum'),
        '42161': (os.getenv('ARB_RPC_ENDPOINT'), 'arbitrum'),
        '10': (os.getenv('OP_RPC_ENDPOINT'), 'optimism'),
        '43114': ('https://api.avax.network/ext/bc/C/rpc', 'avalanche')
    }
    
    if network_id not in network_endpoints:
        return jsonify({'error': 'Unsupported network ID'}), 400
    
    url, network_name = network_endpoints[network_id]
    name = '{}/explanations/{}.json'.format(network_name, tx_hash)
    blob = GCS_BUCKET.blob(name)

    # If cached result exists, return it
    if blob.exists():
        result = blob.download_as_string().decode('utf-8')
        return jsonify({'result': json.loads(result)}), 200
    
    # Query the RPC API to get the transaction details
    if network_id == '43114':  # Avalanche uses a different API endpoint
        url = f"{url}?txhash={tx_hash}"
    else:
        url = f"{url}/getTransactionReceipt?txhash={tx_hash}"
    
    response = requests.get(url)
    
    if response.status_code == 200:
        tx_data = response.json()
        
        # Prepare the transaction data for simulation
        transaction = {
            "hash": tx_data["transactionHash"],
            "block_number": tx_data["blockNumber"],
            "from_address": tx_data["from"],
            "to_address": tx_data["to"],
            "gas": tx_data["gasUsed"],
            "gas_price": tx_data["effectiveGasPrice"],
            "value": tx_data["value"],
            "input": tx_data["input"],
            "transaction_index": tx_data["transactionIndex"]
        }
        
        result = await simulate_and_explain_txs([transaction], network_name, system_prompt, model, max_tokens, temperature)

        # Cache the result in GCS
        if result and len(result) > 0:
            blob.upload_from_string(json.dumps(result[0]))
        else:
            return jsonify({'error': 'Error processing transaction'}), 500
        
        return jsonify({'result': result[0]}), 200
    else:
        return jsonify({'error': 'Transaction not found'}), 404

if __name__ == '__main__':
    app.run()