import os
import json
import requests
from flask import Flask, request, jsonify, g
from flask_httpauth import HTTPTokenAuth
from werkzeug.exceptions import BadRequest, Unauthorized, NotFound, InternalServerError
from anthropic import AsyncAnthropic
from google.cloud import storage
from explain import explain_transaction
from simulate import simulate_transaction
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
auth = HTTPTokenAuth(scheme='Bearer')
storage_client = storage.Client()
GCS_BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')
GCS_BUCKET = storage_client.bucket(GCS_BUCKET_NAME)
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
ANTHROPIC_CLIENT = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
DEFAULT_MODEL = os.getenv('DEFAULT_MODEL')
DEFAULT_MAX_TOKENS = 2000
DEFAULT_TEMPERATURE = 0
DEFAULT_SYSTEM_PROMPT = None

with open('system_prompt.txt', 'r') as file:
    DEFAULT_SYSTEM_PROMPT = file.read()

@auth.verify_token
def verify_token(token):
    if token == os.getenv('API_TOKEN'):
        g.current_user = 'authenticated'
        return True
    return False

async def simulate_txs(transactions, network):
    result = []
    for transaction in transactions:
        try:
            condensed_simulation = await simulate_transaction(
                transaction.hash, transaction.block_number, transaction.from_address, 
                transaction.to_address, transaction.gas, transaction.gas_price, 
                transaction.value, transaction.input, transaction.transaction_index, network
            )
            result.append(condensed_simulation)
        except Exception as e:
            raise InternalServerError(f"Error simulating transaction: {str(e)}")
    return result

async def explain_txs(transactions, system_prompt, model, max_tokens, temperature):
    result = []
    for transaction in transactions:
        try:
            explanation = await explain_transaction(
                ANTHROPIC_CLIENT, transaction, system_prompt, model, max_tokens, temperature
            )
            result.append(explanation)
        except Exception as e:
            raise InternalServerError(f"Error explaining transaction: {str(e)}")
    return result

async def simulate_and_explain_txs(transactions, network, system_prompt, model, max_tokens, temperature):
    result = []
    for transaction in transactions:
        try:
            condensed_simulation = await simulate_transaction(
                transaction.hash, transaction.block_number, transaction.from_address,
                transaction.to_address, transaction.gas, transaction.gas_price,
                transaction.value, transaction.input, transaction.transaction_index, network
            )
            explanation = await explain_transaction(
                ANTHROPIC_CLIENT, condensed_simulation, system_prompt, model, max_tokens, temperature
            )
            result.append(explanation)
        except Exception as e:
            raise InternalServerError(f"Error simulating and explaining transaction: {str(e)}")
    return result

@app.route('/v1/transaction/simulate', methods=['POST'])
@auth.login_required
async def simulate_transactions():
    try:
        data = request.get_json(force=True)
        transactions = data['transactions']
        network = data.get('network', 'ethereum')
        result = await simulate_txs(transactions, network)
        return jsonify({'result': result}), 200
    except (KeyError, BadRequest):
        raise BadRequest("Invalid request payload")

@app.route('/v1/transaction/explain', methods=['POST'])
@auth.login_required  
async def explain_transactions():
    try:
        data = request.get_json(force=True)
        transactions = data['transactions']
        system_prompt = data.get('system', DEFAULT_SYSTEM_PROMPT)
        model = data.get('model', DEFAULT_MODEL)
        max_tokens = data.get('max_tokens', DEFAULT_MAX_TOKENS)
        temperature = data.get('temperature', DEFAULT_TEMPERATURE)
        result = await explain_txs(transactions, system_prompt, model, max_tokens, temperature)
        return jsonify({'result': result}), 200
    except (KeyError, BadRequest):
        raise BadRequest("Invalid request payload")

@app.route('/v1/transaction/simulate_and_explain', methods=['POST'])
@auth.login_required
async def simulate_and_explain_transactions():
    try:
        data = request.get_json(force=True)
        transactions = data['transactions']
        network = data.get('network', 'ethereum')
        system_prompt = data.get('system', DEFAULT_SYSTEM_PROMPT)
        model = data.get('model', DEFAULT_MODEL)
        max_tokens = data.get('max_tokens', DEFAULT_MAX_TOKENS)
        temperature = data.get('temperature', DEFAULT_TEMPERATURE)
        result = await simulate_and_explain_txs(transactions, network, system_prompt, model, max_tokens, temperature)
        return jsonify({'result': result}), 200
    except (KeyError, BadRequest):
        raise BadRequest("Invalid request payload")

@app.route('/v1/transaction', methods=['POST'])
@auth.login_required
async def get_transaction():
    try:
        data = request.get_json(force=True)
        tx_hash = data.get('tx_hash')
        network_id = data.get('network_id')
        system_prompt = data.get('system', DEFAULT_SYSTEM_PROMPT)
        model = data.get('model', DEFAULT_MODEL)
        max_tokens = data.get('max_tokens', DEFAULT_MAX_TOKENS)
        temperature = data.get('temperature', DEFAULT_TEMPERATURE)

        if not network_id:
            raise BadRequest('Missing network ID')
        
        if not tx_hash:
            raise BadRequest('Missing transaction hash')
        
        network_endpoints = {
            '1': (os.getenv('ETH_RPC_ENDPOINT'), 'ethereum'),
            '42161': (os.getenv('ARB_RPC_ENDPOINT'), 'arbitrum'),
            '10': (os.getenv('OP_RPC_ENDPOINT'), 'optimism'),
            '43114': ('https://api.avax.network/ext/bc/C/rpc', 'avalanche')
        }
        
        if network_id not in network_endpoints:
            raise BadRequest('Unsupported network ID')
        
        url, network_name = network_endpoints[network_id]
        name = f"{network_name}/explanations/{tx_hash}.json"
        blob = GCS_BUCKET.blob(name)

        if blob.exists():
            result = blob.download_as_string().decode('utf-8')
            return jsonify({'result': json.loads(result)}), 200
        
        if network_id == '43114':
            url = f"{url}?txhash={tx_hash}"
        else:
            url = f"{url}/getTransactionReceipt?txhash={tx_hash}"
        
        response = requests.get(url)
        
        if response.status_code == 200:
            tx_data = response.json()
            
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

            if result and len(result) > 0:
                blob.upload_from_string(json.dumps(result[0]))
            else:
                raise InternalServerError('Error processing transaction')
            
            return jsonify({'result': result[0]}), 200
        elif response.status_code == 404:
            raise NotFound('Transaction not found')
        else:
            raise InternalServerError('Error querying transaction')

    except (KeyError, BadRequest) as e:
        raise BadRequest(str(e))
    except InternalServerError as e:
        raise InternalServerError(str(e))

@app.errorhandler(400)
def handle_bad_request(e):
    return jsonify({'error': 'Bad Request', 'message': e.description}), 400

@app.errorhandler(401)
def handle_unauthorized(e):
    return jsonify({'error': 'Unauthorized', 'message': 'Invalid or missing authentication token'}), 401

@app.errorhandler(404)
def handle_not_found(e):
    return jsonify({'error': 'Not Found', 'message': e.description}), 404

@app.errorhandler(500)
def handle_internal_server_error(e):
    return jsonify({'error': 'Internal Server Error', 'message': e.description}), 500

if __name__ == '__main__':
    app.run()