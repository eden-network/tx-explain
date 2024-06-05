import os
import aiohttp
import asyncio
import requests
import json
import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery, storage
from groq import Groq
from web3 import Web3, AsyncWeb3
from flipside import Flipside
from label import fetch_address_labels
import time

load_dotenv()

# Getting the transaction receipt
async def get_tx_receipt(tx_hash, web3):
    try:
        tx_receipt = await web3.eth.get_transaction_receipt(tx_hash)
        tx_receipt = dict(tx_receipt)
        if tx_receipt['to'] == None:
            print('Is Contract Creation')
        return tx_receipt
    except Exception as e:
        print("Error at get_tx_receipt: ", e)

# Getting the transaction details
async def get_tx_details(tx_hash, web3):
    try:
        tx_details = await web3.eth.get_transaction(tx_hash)
        tx_details = dict(tx_details)
        if 'type' in tx_details:
            try:
                transaction_type = int(tx_details['type'], 16)
            except:
                transaction_type = int(tx_details['type'])
            tx_details['type'] = transaction_type
        else:
            tx_details['type'] = 0  # Legacy transactions do not have a 'type' field
        return tx_details
    except Exception as e:
        print("Error at get_tx_details: ", e)

# Querying the Zero MEV api to check if the transaction at specified index is a MEV transaction
async def mev_status(tx_index, tx_block):
    try:
        url = f'https://data.zeromev.org/v1/mevBlock?block_number={tx_block}&count=1'
        mev_types = ['arb', 'frontrun', 'backrun', 'sandwich', 'liquid']
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response = await response.json()
                response = [item for item in response if item["tx_index"] == tx_index and item["mev_type"] in mev_types]

                if response:
                    response_printable = json.dumps(response, indent=4)
                    tx_mev_status = "MEV status: This transaction is a MEV transaction: " + "\n" + response_printable
                    print("Is MEV.")
                else:
                    tx_mev_status = "MEV status: This transaction is NOT a MEV transaction"

                return tx_mev_status
    except Exception as e:
        print("Error at mev_status: ", e)
        return "/"

async def augment_summary (tx_summary, tx_tenderly_object, transaction_hash, web3, flipside, network):
    try:
        # Augmenting for Blob transactions
        tx_details = await get_tx_details(transaction_hash, web3)
        if tx_details['type'] == 3:
            blob_status = "Transaction type: " + str(tx_details['type']) + ". This transaction is a Blob transaction."
        else:
            blob_status = "Transaction type: " + str(tx_details['type']) + ". This transaction is NOT a Blob transaction."


        # Augmenting for Contract Creation transactions
        tx_receipt = await get_tx_receipt(transaction_hash, web3)
        if tx_receipt['to'] == None:
            contract_creations_status = "This transaction is Contract Creation transaction"
        else:
            contract_creations_status = "This transaction is NOT a Contract Creation transaction."

        # Augmenting for MEV
        if network == 'ethereum':
            tx_index = tx_details['transactionIndex']
            tx_block = tx_details['blockNumber']
            tx_mev_status = await mev_status(tx_index, tx_block)
        else:
            tx_mev_status = ""

        # Augmenting for contract names

        contract_labels_json = fetch_address_labels(tx_tenderly_object, flipside, network)
        tx_summary_tagged = tx_summary + str(contract_labels_json)

        # Augmenting for functions called
        #tx_summary_augmented = add_functions(tx_summary_tagged, tx_tenderly_object)

        # Applying to summary
        tx_summary_augmented = tx_summary_tagged + "\n" + blob_status + "\n" + contract_creations_status + "\n" + tx_mev_status
        return tx_summary_augmented
    
    except Exception as e:
        print("Error in augment_summary: ", e)
        print("Returning unaugmented summary.")

        return tx_summary

# Formatting the system prompt template with config files
def prompt_structured_3 (label_list, probability_config, tx_summary, res_format):
    try:
        system_prompt_file = os.environ.get("CATEGORIZATION_SYSTEM_PROMPT_FILE")
        with open(system_prompt_file, 'r') as file:
            system_prompt_template = file.read()

        prompt = system_prompt_template.format(label_list,probability_config,res_format,tx_summary)
            
        return prompt
    except Exception as e:
        print("Exception at prompt_structured_3: ", e)

# Running the model
async def run_model (client, model, prompt):
    print("Initiating model")
    try:
        chat_completion = client.chat.completions.create(
            model= model, 
            messages=[{"role": "user", "content": prompt}]
        )
        print("Chat completion: \n", chat_completion)
        return chat_completion
    except Exception as e:
        print("Error at run_model: ", e)

async def classify_tx (transaction, simulation, explanation, network, rpc_endpoint):

    # Load environment variables
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
    LABELS_FILE_PATH = os.environ.get("LABELS_FILE_PATH")
    PROBABILITY_CONFIG_PATH = os.environ.get("PROBABILITY_CONFIG_PATH")
    OUTPUT_FORMAT_PATH = os.environ.get("OUTPUT_FORMAT_PATH")
    MODEL = os.environ.get("CATEGORIZATION_MODEL")
    #ETH_RPC_ENDPOINT = os.environ.get("ETH_RPC_ENDPOINT")

    flipside_api_key = os.getenv('FLIPSIDE_API_KEY')
    flipside_endpoint_url = os.getenv('FLIPSIDE_ENDPOINT_URL')
    flipside = Flipside(flipside_api_key, flipside_endpoint_url)

    # Config clients
    groq_client = Groq(api_key=GROQ_API_KEY,)
    #web3 = Web3(Web3.HTTPProvider(ETH_RPC_ENDPOINT))
    web3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_endpoint))

    # Load the labels config file
    with open(LABELS_FILE_PATH, "r") as json_file:
            label_list = json.load(json_file)

    # Load the probability config file
    with open(PROBABILITY_CONFIG_PATH, "r") as json_file:
        probability_config = json.load(json_file)

    # Load the result format file
    with open(OUTPUT_FORMAT_PATH, "r") as json_file:
        res_format = json.load(json_file)
    
    # Specify the model
    model = MODEL

    try:
        tx_summary_augmented = await augment_summary(explanation, simulation, transaction, web3, flipside, network)
        prompt = prompt_structured_3(label_list, probability_config, tx_summary_augmented, res_format)
        
        output = await run_model(groq_client, model, prompt)

        if output.choices:
            # Checking if the output is alright; re-run the classification is off. 
            # Necessary due to very rare occurences of model hallucination.
            if 'labels' not in output.choices[0].message.content:
                print("-- Problem tx: ", transaction)
                print("----- Problem output: ")
                print(output.choices[0].message.content)

        return output

    except Exception as e:
        print("Error at classify_tx: ", e)

async def categorize (tx_hash, network, rpc_endpoint):
    try:
        print("--- Initiating categorization.")
        start_time = time.time()

        # Read stored simulation and explanation for tx_hash from buckets
        storage_client = storage.Client()
        bucket_name = os.getenv("GCS_BUCKET_NAME")
        bucket = storage_client.bucket(bucket_name)

        # Check if the categorization result already exists
        print("Checking if the transaction has been categorized...")
        category_blob = bucket.blob(f'{network}/transactions/categories/{tx_hash}.json')
        if category_blob.exists():
            print("Transaction ", tx_hash, " has already been categorized. Loading categories from buckets.")
            print(category_blob.download_as_text())
            output = category_blob.download_as_text().replace("'",'"')
            return json.loads(output)

        # Read the simulation blob
        print("Reading the simulation data...")
        simulation_blob = bucket.blob(f'{network}/transactions/simulations/trimmed/{tx_hash}.json')
        simulation_data = json.loads(simulation_blob.download_as_text())

        # Read the explanation blob
        print("Reading the explanation data...")
        #explanation_blob = bucket.blob(f'{network}/transactions/explanations/{tx_hash}.json')
        
        # I am setting the bucket for reading explanation to Ethereum subfolder because all explanations are stored there until the issue is fixed
        explanation_blob = bucket.blob(f'ethereum/transactions/explanations/{tx_hash}.json')
        explanation_data = explanation_blob.download_as_text()

        max_retries = 2
        for attempt in range(max_retries):
            output = await classify_tx(tx_hash, simulation_data, explanation_data, network, rpc_endpoint)
            if 'labels' in output.choices[0].message.content:
                break
            else:
                print(f"Attempt {attempt + 1} failed. Retrying...")
        
        if 'labels' not in output.choices[0].message.content:
            raise ValueError("Failed to categorize the transaction after multiple attempts.")
        
        output = output.choices[0].message.content.replace("'",'"')
        print("Printing output... \n", output)

        # Writing categories in the bucket
        output_blob = bucket.blob(f'{network}/transactions/categories/{tx_hash}.json')
        output_blob.upload_from_string(output)

        end_time = time.time()
        elapsed_time = end_time - start_time

        print(f"Categorization elapsed time: {elapsed_time} seconds")
        print("Categories: ", output)
        return json.loads(output)
    except Exception as e:
        print("Error at categorize: ", e)
        return json.loads('{"labels":[],"probabilities":[]}')