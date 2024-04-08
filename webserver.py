import os
import time
import requests
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from anthropic import AsyncAnthropic
from google.cloud import storage
from explain import explain_transaction, get_cached_explanation
from simulate import simulate_transaction, get_cached_simulation
from dotenv import load_dotenv
from typing import List, Optional, Any
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI()
origins = ["*"]
CORS_ALLOWED_ORIGINS = os.getenv('CORS_ALLOWED_ORIGINS')
if CORS_ALLOWED_ORIGINS:
    origins = CORS_ALLOWED_ORIGINS.split(',')
    print(f"Allowed origins: {origins}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
auth_scheme = HTTPBearer()

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

class Transaction(BaseModel):
    hash: str
    block_number: int
    from_address: str
    to_address: str
    gas: int
    value: str
    input: str
    transaction_index: int

class TransactionRequest(BaseModel):
    tx_hash: str
    network_id: str
    system: str = DEFAULT_SYSTEM_PROMPT
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    force_refresh: bool = False

class SimulateTransactionsRequest(BaseModel):
    transactions: list[Transaction]
    network: str = 'ethereum'
    force_refresh: bool = False

class ExplainTransactionsRequest(BaseModel):
    transactions: list[Any]
    network: str = 'ethereum'
    system: str = DEFAULT_SYSTEM_PROMPT
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    force_refresh: bool = False

async def authenticate(authorization: HTTPAuthorizationCredentials = Depends(auth_scheme)):
    token = authorization.credentials
    if token != os.getenv('API_TOKEN'):
        raise HTTPException(status_code=401, detail="Invalid token")
    return token

async def simulate_txs(transactions, network, force_refresh=False):
    result = []
    for transaction in transactions:
        if not force_refresh:
            tx_hash = transaction.get('hash')
            if tx_hash:
                cached_simulation = await get_cached_simulation(tx_hash, network)
                if cached_simulation:
                    print(f"Using cached simulation for {tx_hash}")
                    result.append(cached_simulation)
                    continue
        try:
            trimmed_simulation = await simulate_transaction(
                transaction.hash, transaction.block_number, transaction.from_address,
                transaction.to_address, transaction.gas,
                transaction.value, transaction.input, transaction.transaction_index, network
            )
            result.append(trimmed_simulation)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error simulating transaction: {str(e)}")
    return result

async def explain_txs(transactions, network, system_prompt, model, max_tokens, temperature, force_refresh=False):
    for transaction in transactions:
        if not force_refresh:
            tx_hash = transaction.get('hash')
            if tx_hash:
                cached_explanation = await get_cached_explanation(tx_hash, network)
                if cached_explanation:
                    explanation = cached_explanation.get('result')
                    if explanation:    
                        print(f"Using cached explanation for {tx_hash}")
                        lines = explanation.splitlines()
                        for line in lines:
                            words = line.split()
                            for i, word in enumerate(words):
                                if i < len(words):
                                    yield word + " "
                                    time.sleep(0.01)
                                else:
                                    yield word
                            yield "\n"
                        continue
        try:
            async for item in explain_transaction(
                ANTHROPIC_CLIENT, transaction, network=network, system_prompt=system_prompt, model=model, max_tokens=max_tokens, temperature=temperature
            ):
                yield item
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error explaining transaction: {str(e)}")

@app.post("/_ah/warmup")
async def warmup():
    return {"status": "ok"}
    
@app.post("/v1/transaction/fetch")
async def get_transaction(request: TransactionRequest, _: str = Depends(authenticate)):
    try:
        if not request.network_id:
            raise HTTPException(status_code=400, detail='Missing network ID')
        if not request.tx_hash:
            raise HTTPException(status_code=400, detail='Missing transaction hash')

        network_endpoints = {
            '1': (os.getenv('ETH_RPC_ENDPOINT'), 'ethereum'),
            '42161': (os.getenv('ARB_RPC_ENDPOINT'), 'arbitrum'),
            '10': (os.getenv('OP_RPC_ENDPOINT'), 'optimism'),
            '43114': ('https://api.avax.network/ext/bc/C/rpc', 'avalanche')
        }

        if request.network_id not in network_endpoints:
            raise HTTPException(status_code=400, detail='Unsupported network ID')

        url = network_endpoints[request.network_id]

        body = {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "eth_getTransactionByHash",
            "params": [request.tx_hash]
        }

        response = requests.post(url, json=body)

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail='Transaction not found')
        else:
            raise HTTPException(status_code=500, detail='Error fetching transaction')

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@app.post("/v1/transaction/simulate")
async def simulate_transactions(request: SimulateTransactionsRequest, _: str = Depends(authenticate)):
    try:
        result = await simulate_txs(request.transactions, request.network, request.force_refresh)
        return {"result": result}
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid request payload")

@app.post("/v1/transaction/explain")
async def explain_transactions(request: ExplainTransactionsRequest, _: str = Depends(authenticate)):
    try:
        return StreamingResponse(
            explain_txs(request.transactions, request.network, request.system, request.model, request.max_tokens, request.temperature, request.force_refresh),
            media_type="text/plain"
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid request payload")

@app.post("/v1/transaction/fetch_and_simulate")
async def fetch_and_simulate_transaction(request: TransactionRequest, _: str = Depends(authenticate)):
    try:
        if not request.network_id:
            raise HTTPException(status_code=400, detail='Missing network ID')
        if not request.tx_hash:
            raise HTTPException(status_code=400, detail='Missing transaction hash')

        network_endpoints = {
            '1': (os.getenv('ETH_RPC_ENDPOINT'), 'ethereum'),
            '42161': (os.getenv('ARB_RPC_ENDPOINT'), 'arbitrum'),
            '10': (os.getenv('OP_RPC_ENDPOINT'), 'optimism'),
            '43114': ('https://api.avax.network/ext/bc/C/rpc', 'avalanche')
        }

        if request.network_id not in network_endpoints:
            raise HTTPException(status_code=400, detail='Unsupported network ID')

        url, network_name = network_endpoints[request.network_id]
        cached_simulation = await get_cached_simulation(request.tx_hash, network_name)
        if cached_simulation and not request.force_refresh:
            return {"result": cached_simulation}

        body = {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "eth_getTransactionByHash",
            "params": [request.tx_hash]
        }

        response = requests.post(url, json=body)

        if response.status_code == 200:
            resJson = response.json()
            tx_data = resJson.get('result')
            if not tx_data or not isinstance(tx_data, dict) or not tx_data.get('blockNumber'):
                raise HTTPException(status_code=404, detail='Transaction not found')

            transaction = Transaction(
                hash=tx_data["hash"],
                block_number=int(tx_data["blockNumber"], 16),
                from_address=tx_data["from"],
                to_address=tx_data["to"],
                gas=int(tx_data["gas"], 16),
                value=str(int(tx_data["value"], 16)),
                input=tx_data["input"],
                transaction_index=int(tx_data["transactionIndex"], 16)
            )
            result = await simulate_txs([transaction], network_name, True)

            return {"result": result[0]}
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail='Transaction not found')
        else:
            raise HTTPException(status_code=500, detail='Error fetching transaction')

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )

if __name__ == "__main__":
    uvicorn.run("webserver:app", host="0.0.0.0", port=int(os.getenv('PORT')), log_level="debug", reload=True)