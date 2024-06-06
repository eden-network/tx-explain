import os
import time
import json
import requests
import aiohttp
import uvicorn
import google.auth
import gspread
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from anthropic import AsyncAnthropic
from google.cloud import storage
from explain import explain_transaction, get_cached_explanation
from simulate import simulate_transaction, get_cached_simulation
from simulate_pending import simulate_pending_transaction_tenderly
from dotenv import load_dotenv
from typing import List, Optional, Any
from pydantic import BaseModel, Field, validator
from tenacity import retry, stop_after_attempt, wait_exponential
from categorize import categorize  # Import categorize function
import time

load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
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

SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
CREDENTIALS, PROJECT_ID = google.auth.default(scopes=SCOPES)
STORAGE_CLIENT = storage.Client()
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
GOOGLE_WORKSHEET_NAME = os.getenv('GOOGLE_WORKSHEET_NAME')
GCS_BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')
GCS_BUCKET = STORAGE_CLIENT.bucket(GCS_BUCKET_NAME)
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
ANTHROPIC_CLIENT = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
DEFAULT_MODEL = os.getenv('DEFAULT_MODEL')
DEFAULT_MAX_TOKENS = 2000
DEFAULT_TEMPERATURE = 0
DEFAULT_SYSTEM_PROMPT = None
RECAPTCHA_TIMEOUT = int(os.getenv('RECAPTCHA_TIMEOUT', 3))
RECAPTCHA_SECRET_KEY = os.getenv('RECAPTCHA_SECRET_KEY', '')


with open('system_prompt.txt', 'r') as file:
    DEFAULT_SYSTEM_PROMPT = file.read()

class CategorizationRequest(BaseModel):
    tx_hash: str
    network_id: str
    recaptcha_token: str

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
    recaptcha_token: str

class PendingTransactionRequest(BaseModel):
    network_id: str
    tx_hash: str
    block_number: int
    from_address: str
    to_address: str
    gas: int
    value: str
    input: str
    transaction_index: int
    system: str = DEFAULT_SYSTEM_PROMPT
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    force_refresh: bool = False
    recaptcha_token: str

class SimulateTransactionsRequest(BaseModel):
    transactions: list[Transaction]
    network: str = 'ethereum'
    force_refresh: bool = False
    recaptcha_token: str

class ExplainTransactionsRequest(BaseModel):
    transactions: list[Any]
    network: str = 'ethereum'
    system: str = DEFAULT_SYSTEM_PROMPT
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    force_refresh: bool = False
    recaptcha_token: str

class FeedbackForm(BaseModel):
    date: str
    network: str
    txHash: str
    explanation: str
    model: str
    systemPrompt: str
    simulationData: str
    comments: str
    accuracy: int = Field(gt=0, lt=6)  # Ensures accuracy is between 1 and 5
    quality: int = Field(gt=0, lt=6)  # Ensures quality is between 1 and 5
    explorer: Optional[str] = None  # Will be set based on network and txHash,


    @validator('explorer', pre=True, always=True)
    def set_explorer_url(cls, v, values):
        network = values.get('network', '').lower()
        tx_hash = values.get('txHash', '')
        base_urls = {
            'ethereum': 'https://etherscan.io/tx/',
            'avalanche': 'https://snowtrace.io/tx/',
            'optimism': 'https://optimistic.etherscan.io/tx/',
            'arbitrum': 'https://arbiscan.io/tx/',
            'base': 'https://basescan.org/tx/',
            'blast': 'https://blastscan.io/tx/',
            'mantle': 'https://mantlescan.info/tx/'
        }
        explorer_base_url = base_urls.get(network)
        if explorer_base_url and tx_hash:
            return f"{explorer_base_url}{tx_hash}"
        return v

async def authenticate(authorization: HTTPAuthorizationCredentials = Depends(auth_scheme)):
    token = authorization.credentials
    if token != os.getenv('API_TOKEN'):
        raise HTTPException(status_code=401, detail="Invalid token")
    return token

async def verify_recaptcha(token: str) -> bool:
    if os.getenv('ENV') == 'local':
        return True
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f'https://www.google.com/recaptcha/api/siteverify?secret={RECAPTCHA_SECRET_KEY}&response={token}', timeout=RECAPTCHA_TIMEOUT) as response:
                data = await response.json()
                print(data)
                return data.get('success', False)
        except aiohttp.ClientError:
            print("reCAPTCHA request timed out. Proceeding with the request.")
            return True
        
async def fetch_transaction(url, body):
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body) as response:
            return await response.json()

def split_long_text(text, max_length=50000):
    return [text[i:i+max_length] for i in range(0, len(text), max_length)]

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1))
async def submit_feedback_with_retry(feedback: FeedbackForm):
    client = gspread.authorize(CREDENTIALS)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_WORKSHEET_NAME)
    
    simulation_data_parts = split_long_text(feedback.simulationData)
    
    values = [[
        feedback.date,
        feedback.network,
        feedback.txHash,
        feedback.explorer,
        feedback.explanation,
        feedback.model,
        feedback.systemPrompt,
        simulation_data_parts[0] if simulation_data_parts else "",
        feedback.accuracy,
        feedback.quality,
        feedback.comments
    ]]
    
    if len(simulation_data_parts) > 1:
        for part in simulation_data_parts[1:]:
            values.append(["", "", "", "", "", "", "", part, "", "", ""])
    
    sheet.append_rows(values)

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

async def simulate_pending_txs(transactions, network, store_result, force_refresh=False):
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
            trimmed_simulation = await simulate_pending_transaction_tenderly(
                transaction.hash, transaction.block_number, transaction.from_address,
                transaction.to_address, transaction.gas,
                transaction.value, transaction.input, transaction.transaction_index, network,
                store_result
            )
            result.append(trimmed_simulation)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error simulating transaction: {str(e)}")
    return result

async def explain_txs(transactions, network, system_prompt, model, max_tokens, temperature, store_result, force_refresh=False):
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
                ANTHROPIC_CLIENT, transaction, network=network, system_prompt=system_prompt, model=model, max_tokens=max_tokens, temperature=temperature, store_result=store_result
            ):
                yield item
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error explaining transaction: {str(e)}")

@app.get("/")
async def root():
    return {"status": "ok"}
    
@app.post("/v1/transaction/fetch")
async def get_transaction(request: TransactionRequest, _: str = Depends(authenticate)):
    try:
        if not request.network_id:
            raise HTTPException(status_code=400, detail='Missing network ID')
        if not request.tx_hash:
            raise HTTPException(status_code=400, detail='Missing transaction hash')

        global network_endpoints

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
        is_human = await verify_recaptcha(request.recaptcha_token)
        if not is_human:
            raise HTTPException(status_code=400, detail="Bot detected")
        result = await simulate_txs(request.transactions, request.network, request.force_refresh)
        return {"result": result}
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid request payload")

@app.post("/v1/transaction/explain")
async def explain_transactions(request: ExplainTransactionsRequest, _: str = Depends(authenticate)):
    try:
        is_human = await verify_recaptcha(request.recaptcha_token)
        if not is_human:
            raise HTTPException(status_code=400, detail="Bot detected")
        msg = {
            "action": "explainRequested",
            "transactions": request.transactions,
            "network": request.network,
            "system": request.system,
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature
        }
        print(json.dumps(msg))

        # Setting storage status to true
        store_result = True
        return StreamingResponse(
            explain_txs(request.transactions, request.network, request.system, request.model, request.max_tokens, request.temperature, store_result ,request.force_refresh),
            media_type="text/plain"
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid request payload")

@app.post("/v1/transaction/fetch_and_simulate")
async def fetch_and_simulate_transaction(request: TransactionRequest, _: str = Depends(authenticate)):
    try:
        print (request)
        is_human = await verify_recaptcha(request.recaptcha_token)
        if not is_human:
            raise HTTPException(status_code=400, detail="Bot detected")

        if not request.network_id:
            raise HTTPException(status_code=400, detail='Missing network ID')
        if not request.tx_hash:
            raise HTTPException(status_code=400, detail='Missing transaction hash')

        global network_endpoints

        if request.network_id not in network_endpoints:
            raise HTTPException(status_code=400, detail='Unsupported network ID')
        msg = {
            "action": "fetchAndSimulate",
            "txHash": request.tx_hash,
            "network": network_endpoints[request.network_id][1]
        }
        print(json.dumps(msg))
        url, network_name = network_endpoints[request.network_id]
        cached_simulation = await get_cached_simulation(request.tx_hash, network_name)
        if not request.force_refresh:
            if cached_simulation:
                return {"result": cached_simulation}

        body = {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "eth_getTransactionByHash",
            "params": [request.tx_hash]
        }

        resJson = await fetch_transaction(url, body)
        tx_data = resJson.get('result')
        if not tx_data or not isinstance(tx_data, dict) or not tx_data.get('blockNumber'):
            raise HTTPException(status_code=404, detail='Transaction not found')
        if tx_data["to"]==None:
            tx_data["to"]=""
            #needed for contract creations
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

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/v1/transaction/simulate_pending")
async def simulate_pending_transaction(request: PendingTransactionRequest, _: str = Depends(authenticate)):
    try:
        is_human = await verify_recaptcha(request.recaptcha_token)
        if not is_human:
            raise HTTPException(status_code=400, detail="Bot detected")

        if not request.network_id:
            raise HTTPException(status_code=400, detail='Missing network ID')
        if not request.tx_hash:
            raise HTTPException(status_code=400, detail='Missing transaction hash')

        global network_endpoints

        if request.network_id not in network_endpoints:
            raise HTTPException(status_code=400, detail='Unsupported network ID')
        msg = {
            "action": "simulatePendingTransaction",
            "txHash": request.tx_hash,
            "network": network_endpoints[request.network_id][1]
        }
        print(json.dumps(msg))
        url, network_name = network_endpoints[request.network_id]
        cached_simulation = await get_cached_simulation(request.tx_hash, network_name)
        if not request.force_refresh:
            if cached_simulation:
                return {"result": cached_simulation}

        body = {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "eth_getTransactionByHash",
            "params": [request.tx_hash]
        }

        transaction = Transaction(
            hash=request.tx_hash,
            block_number=request.block_number,
            from_address=request.from_address,
            to_address=request.to_address,
            gas=request.gas,
            value=request.value,
            input=request.input,
            transaction_index=request.transaction_index
        )
        store_result = True
        result = await simulate_pending_txs([transaction], network_name, store_result, True)
        if "error" in result:
            raise HTTPException(status_code=400, detail=str(result))
        return {"result": result[0]}

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@app.post("/v1/feedback")
async def submit_feedback(feedback: FeedbackForm):
    try:
        msg = {
            "action": "feedbackSubmitted",
            "feedback": feedback.dict()
        }
        print(json.dumps(msg))
        await submit_feedback_with_retry(feedback)
        return {"message": "Feedback submitted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to submit feedback: {str(e)}")

# ---------------------------------------------------------------------------
# No data storing
# No streaming response
# Just text output (the summary)
@app.post("/v1/transaction/snap")
async def simulate_for_snap(request: SnapRequest, _: str = Depends(authenticate)):
    try:
        if not request.network_id:
            raise HTTPException(status_code=400, detail='Missing network ID')
        if not request.tx_hash:
            raise HTTPException(status_code=400, detail='Missing transaction hash')

        global network_endpoints
        network_id=str(int(request.network_id,16))
        if network_id not in network_endpoints:
            raise HTTPException(status_code=400, detail='Unsupported network ID')
        msg = {
            "action": "simulatePendingTransactionSnap",
            "txHash": request.tx_hash,
            "network": network_endpoints[request.network_id][1]
        }

        print(json.dumps(msg))
        url, network_name = network_endpoints[network_id]

        body = {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "eth_getTransactionByHash",
            "params": [request.tx_hash]
        }

        transaction = Transaction(
            hash=request.tx_hash,
            block_number=request.block_number,
            from_address=request.from_address,
            to_address=request.to_address,
            gas=request.gas,
            value=request.value,
            input=request.input,
            transaction_index=request.transaction_index
        )
        
        # Setting store_result to false for both simulations and explanations
        store_result = False

        simulation = await simulate_pending_txs([transaction], network_name, store_result, True)

        if "error" in simulation:
            raise HTTPException(status_code=400, detail=str(simulation))

        force_refresh = False
        explanation = ""
        async for item in explain_txs(simulation, request.network_id, request.system, request.model, request.max_tokens, request.temperature, store_result, force_refresh):
            explanation += item

        return explanation

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))     

@app.post("/v1/transaction/categorize")
async def post_categorize_transaction(request: CategorizationRequest, authorization: HTTPAuthorizationCredentials = Depends(auth_scheme)):
    tx_hash = request.tx_hash
    network_id = request.network_id
    if not tx_hash:
        raise HTTPException(status_code=400, detail="Transaction hash is required")
    if not network_id:
            raise HTTPException(status_code=400, detail='Missing network ID')

    network_endpoints = {
            '1': (os.getenv('ETH_RPC_ENDPOINT'), 'ethereum'),
            '42161': (os.getenv('ARB_RPC_ENDPOINT'), 'arbitrum'),
            '10': (os.getenv('OP_RPC_ENDPOINT'), 'optimism'),
            '43114': ('https://api.avax.network/ext/bc/C/rpc', 'avalanche')
        }

    if network_id not in network_endpoints:
            raise HTTPException(status_code=400, detail='Unsupported network ID')

    msg = {
            "action": "categorize",
            "txHash": request.tx_hash,
            "network": network_endpoints[request.network_id][1]
        }
    print(json.dumps(msg))
    network = network_endpoints[request.network_id][1]
    rpc_endpoint = network_endpoints[request.network_id][0]
    print("Network in categorize: ", network, rpc_endpoint)
    
    if not await verify_recaptcha(request.recaptcha_token):
        raise HTTPException(status_code=401, detail="Invalid reCAPTCHA token")
    
    # Call the categorize function and get the result
    categorize_result = await categorize(tx_hash, network, rpc_endpoint)
    
    return categorize_result
    
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )

if __name__ == "__main__":
    uvicorn.run("webserver:app", host="0.0.0.0", port=int(os.getenv('PORT')), log_level="debug", reload=True)
