import os
import json
import asyncio
import argparse
from dotenv import load_dotenv
from anthropic import AsyncAnthropic
from google.cloud import storage

load_dotenv()  # Load environment variables from .env file

BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')

async def extract_json(string):
    start_index = string.find('{')
    end_index = string.rfind('}')
    if start_index == -1 or end_index == -1:
        return string
    
    result = string[start_index : end_index + 1]    
    return result

async def read_json_files(client, network):
    json_data = []
    bucket = client.bucket(BUCKET_NAME)
    blobs = bucket.list_blobs(prefix=f'{network}/simulations/condensed/')
    for blob in blobs:
        if blob.name.endswith('.json'):
            file_path = blob.name
            results_file_path = file_path.replace(f'{network}/simulations/condensed/', f'{network}/results/')
            if storage.Blob(results_file_path, bucket).exists():
                continue
            data = json.loads(blob.download_as_string())
            if data['m'][0]['f'] in SKIP_FUNCTION_CALLS:
                continue
            json_data.append((file_path, data))
    return json_data

async def explain_transaction(client, payload, system_prompt=None, model="claude-3-haiku-20240307", max_tokens=2000, temperature=0):
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


    try:
        async with client.messages.stream(**request_params) as stream:
            async for word in stream.text_stream:
                yield word
        yield "/n"
    except Exception as e:
        print(f"Error streaming explanation: {str(e)}")

async def write_result_to_bucket(storage_client, file_path, result, network):
    bucket = storage_client.bucket(BUCKET_NAME)
    result_blob = bucket.blob(file_path.replace(f'{network}/simulations/condensed/', f'{network}/results/'))
    result_blob.upload_from_string(json.dumps(result, separators=(',', ':')))

async def process_json_file(anthropic_client, storage_client, file_path, data, network, semaphore, delay_time, system_prompt):
    async with semaphore:
        print(f'Analyzing: {file_path}...')
        response = await explain_transaction(anthropic_client, data, system_prompt)
        if response and response != {}:
            tx_hash = os.path.splitext(os.path.basename(file_path))[0]
            response['tx_hash'] = tx_hash
            await write_result_to_bucket(storage_client, file_path, response, network)
        else:
            print(f'Error processing {file_path}')
        await asyncio.sleep(delay_time)

async def main(network, delay_time, max_concurrent_connections, skip_function_calls, system_prompt_file):
    global SKIP_FUNCTION_CALLS
    SKIP_FUNCTION_CALLS = skip_function_calls

    system_prompt = None
    if system_prompt_file:
        with open(system_prompt_file, 'r') as file:
            system_prompt = file.read()

    storage_client = storage.Client()
    json_data = await read_json_files(storage_client, network)
    
    api_key = os.getenv('ANTHROPIC_API_KEY')
    anthropic_client = AsyncAnthropic(api_key=api_key)
    semaphore = asyncio.Semaphore(max_concurrent_connections)
    
    tasks = []
    for file_path, data in json_data:
        task = asyncio.create_task(process_json_file(anthropic_client, storage_client, file_path, data, network, semaphore, delay_time, system_prompt))
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
    args = parser.parse_args()

    network = args.network
    delay_time = args.delay
    max_concurrent_connections = args.concurrency
    skip_function_calls = args.skip
    system_prompt_file = args.prompt

    asyncio.run(main(network, delay_time, max_concurrent_connections, skip_function_calls, system_prompt_file))