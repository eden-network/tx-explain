import os
import json
import asyncio
import argparse
from datetime import datetime
from dotenv import load_dotenv
from anthropic import AsyncAnthropic
from groq import AsyncGroq
from google.cloud import storage

load_dotenv()  # Load environment variables from .env file

BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')
storage_client = storage.Client()
bucket = storage_client.bucket(BUCKET_NAME)

async def extract_json(string):
    start_index = string.find('{')
    end_index = string.rfind('}')
    if start_index == -1 or end_index == -1:
        return string
    
    result = string[start_index : end_index + 1]    
    return result

async def read_json_files(network):
    json_data = []
    blobs = bucket.list_blobs(prefix=f'{network}/transactions/simulations/trimmed/')
    for blob in blobs:
        if blob.name.endswith('.json'):
            file_path = blob.name
            results_file_path = file_path.replace(f'{network}/transactions/simulations/trimmed/', f'{network}/transactions/explanations/')
            if storage.Blob(results_file_path, bucket).exists():
                continue
            data = json.loads(blob.download_as_string())
            if data['m'][0]['f'] in SKIP_FUNCTION_CALLS:
                continue
            json_data.append((file_path, data))
    return json_data

async def get_cached_explanation(tx_hash, network):
    blob = bucket.blob(f'{network}/transactions/explanations/{tx_hash}.json')
    if blob.exists():
        return json.loads(blob.download_as_string())
    return None

async def explain_transaction(client, payload, network='ethereum', system_prompt=None, model="claude-3-haiku-20240307", max_tokens=2000, temperature=0, store_result=True):
    request_params = {
        'model': model,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(payload)
                    }
                ]
            }
        ]
    }

    if system_prompt:
        request_params['system'] = system_prompt

    explanation = ""
    try:
        async with client.messages.stream(**request_params) as stream:
            async for word in stream.text_stream:
                yield word
                explanation += word
    except Exception as e:
        print(f"Error streaming explanation: {str(e)}")
    
    if store_result:
        print("Writing explanation to buckets...")
        tx_hash = payload.get('hash')
        if explanation and tx_hash:
            try:
                await write_explanation_to_bucket(network, tx_hash, explanation, model)
            except Exception as e:
                print(f'Error uploading explanation for {tx_hash}: {str(e)}')

async def write_explanation_to_bucket(network, tx_hash, explanation, model):
    file_path = f'{network}/transactions/explanations/{tx_hash}.json'
    blob = bucket.blob(file_path)
    updated_at = datetime.now().isoformat()
    blob.upload_from_string(json.dumps({'result': explanation, 'model': model, 'updated_at': updated_at}))

async def process_json_file(anthropic_client, file_path, data, network, semaphore, delay_time, system_prompt, model):
    async with semaphore:
        print(f'Analyzing: {file_path}...')
        explanation = ""
        async for item in explain_transaction(anthropic_client, data, network=network, system_prompt=system_prompt, model=model):
            explanation += item
        if explanation and explanation != "":
            tx_hash = data['hash']
            await write_explanation_to_bucket(network, tx_hash, explanation, model)
        else:
            print(f'Error processing {file_path}')
        await asyncio.sleep(delay_time)

async def main(network, delay_time, max_concurrent_connections, skip_function_calls, system_prompt_file, model):
    global SKIP_FUNCTION_CALLS
    SKIP_FUNCTION_CALLS = skip_function_calls

    system_prompt = None
    if system_prompt_file:
        with open(system_prompt_file, 'r') as file:
            system_prompt = file.read()

    json_data = await read_json_files(network)
    
    api_key = os.getenv('ANTHROPIC_API_KEY')
    anthropic_client = AsyncAnthropic(api_key=api_key)
    semaphore = asyncio.Semaphore(max_concurrent_connections)
    
    tasks = []
    for file_path, data in json_data:
        task = asyncio.create_task(process_json_file(anthropic_client, file_path, data, network, semaphore, delay_time, system_prompt, model))
        tasks.append(task)
    
    await asyncio.gather(*tasks)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Blockchain Transaction Analyzer')
    parser.add_argument('-n', '--network', type=str, default='ethereum', choices=['ethereum', 'arbitrum', 'avalanche', 'optimism'],
                        help='Blockchain network to analyze transactions for (default: ethereum)')
    parser.add_argument('-d', '--delay', type=float, default=1.2,
                        help='Delay time between API requests in seconds (default: 1.2)')
    parser.add_argument('-c', '--concurrency', type=int, default=1,
                        help='Maximum number of concurrent connections to the API (default: 1)')
    parser.add_argument('-s', '--skip', type=str, nargs='+', default=None,
                        help='List of function calls to skip (default: None, suggested: transfer approve transferFrom)')
    parser.add_argument('-p', '--prompt', type=str, default=None,
                        help='Path to the file containing the system prompt (default: None)')
    parser.add_argument('-m', '--model', type=str, default='claude-3-haiku-20240307',
                        help='Model to use for generating explanations (default: claude-3-haiku-20240307)')
    args = parser.parse_args()

    network = args.network
    delay_time = args.delay
    max_concurrent_connections = args.concurrency
    skip_function_calls = args.skip
    system_prompt_file = args.prompt
    model = args.model

    asyncio.run(main(network, delay_time, max_concurrent_connections, skip_function_calls, system_prompt_file, model))
