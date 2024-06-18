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

# Add and remove constraints from user messages
def add_constraint(messages, constraint):
    try:
        if messages and messages[-1]['role'] == 'user':
            latest_user_message = messages[-1]['content']
            for item in latest_user_message:
                if item['type'] == 'text':
                    item['text'] += f" {constraint}"
                    break
        return messages
    except Exception as e:
        print("Error at add_constraint: ", e)

def remove_constraint(messages, constraint):
    try:
        if messages and messages[-1]['role'] == 'user':
            latest_user_message = messages[-1]['content']
            for item in latest_user_message:
                if item['type'] == 'text' and item['text'].endswith(f" {constraint}"):
                    item['text'] = item['text'][:-len(f" {constraint}")]
                    break
        return messages
    except Exception as e:
        print("Error at remove_constraint: ", e)

# Removes entries from 'messages'
async def remove_entries(json_object, num_entries_to_remove):
    try:
        if num_entries_to_remove <= 0:
            return json_object

        print("Removing entries...")
        json_object['messages'] = json_object['messages'][num_entries_to_remove:]
        return json_object
    except Exception as e:
        print("Error at remove_entries: ", e)

async def chat(client, request_params, network, session_id):

    # Adding message constraint
    constraint = """
                Conversation constraint:
                You are not permitted to discuss or answer any questions completely unrelated to the provided transaction or blockchain technology.
                - You absolutely cannot answer any questions about general knowledge topics like geography, history, 
                    popular culture, etc. unless directly relevant to blockchain technology or explaining the transaction.
                - If the user asks a question unrelated to the transaction details or blockchain, you must respond with the following and nothing else:
                    "Sorry, I can only assist you with questions directly related to the provided transaction details or general blockchain concepts. 
                    For example, I could explain gas fees, smart contracts, or consensus mechanisms if relevant to this transaction. 
                    But I cannot answer questions unrelated to blockchain technology. 
                    Is there anything else about this transaction you would like to know?"
                """
    request_params['messages'] = add_constraint(request_params['messages'], constraint)

    try:
        response = ""
        async with client.messages.stream(**request_params) as stream:
            async for item in stream:
                usage = item.message.usage if hasattr(item, 'message') and hasattr(item.message, 'usage') else None
                usage = usage.input_tokens
                async for word in stream.text_stream:
                    yield word, usage
                    response += word

    except Exception as e:
        error_message = str(e)
        if "prompt is too long" in error_message:
            response = ""
            request_params = await remove_entries(request_params, 8)

            async with client.messages.stream(**request_params) as stream:
                async for item in stream:
                    usage = item.message.usage if hasattr(item, 'message') and hasattr(item.message, 'usage') else None
                    usage = usage.input_tokens
                    async for word in stream.text_stream:
                        yield word, usage
                        response += word
            
    except Exception as e:
        print(f"Error streaming response: {str(e)}")

    # Removing message constraint
    request_params['messages'] = remove_constraint(request_params['messages'], constraint)

    request_params['system'] = json.loads(request_params['system']) # Back to json

    request_params["messages"].append({"role": "assistant", "content": [{"type": "text", "text": json.dumps(response)}]})
    
    if response:
        print("Writing chat to buckets...")
        try:
            file_path = f'{network}/transactions/chat_logs/chat_{session_id}.json'
            blob = bucket.blob(file_path)
            blob.upload_from_string(json.dumps(request_params, indent = 4))
        except Exception as e:
            print(f'Error uploading chat for chat {session_id}: {str(e)}')
            
async def questions(client, request_params, network, session_id):

    try:
        response = ""
        async with client.messages.stream(**request_params) as stream:
            async for item in stream:
                usage = item.message.usage if hasattr(item, 'message') and hasattr(item.message, 'usage') else None
                usage = usage.input_tokens
                async for word in stream.text_stream:
                    yield word, usage
                    response += word

    except Exception as e:
        error_message = str(e)
        if "prompt is too long" in error_message:
            response = ""
            request_params = await remove_entries(request_params, 8)

            async with client.messages.stream(**request_params) as stream:
                async for item in stream:
                    usage = item.message.usage if hasattr(item, 'message') and hasattr(item.message, 'usage') else None
                    usage = usage.input_tokens
                    async for word in stream.text_stream:
                        yield word, usage
                        response += word
            
    except Exception as e:
        print(f"Error streaming response: {str(e)}")

    # Removing message constraint
    request_params['messages'] = remove_constraint(request_params['messages'], constraint)

    request_params['system'] = json.loads(request_params['system']) # Back to json

    request_params["messages"].append({"role": "assistant", "content": [{"type": "text", "text": json.dumps(response)}]})
    
    if response:
        print("Writing chat to buckets...")
        try:
            file_path = f'{network}/transactions/chat_logs/chat_{session_id}.json'
            blob = bucket.blob(file_path)
            blob.upload_from_string(json.dumps(request_params, indent = 4))
        except Exception as e:
            print(f'Error uploading chat for chat {session_id}: {str(e)}')
            
async def write_explanation_to_bucket(network, tx_hash, explanation, model):
    file_path = f'{network}/transactions/explanations/{tx_hash}.json'
    blob = bucket.blob(file_path)
    updated_at = datetime.now().isoformat()
    blob.upload_from_string(json.dumps({'result': explanation, 'model': model, 'updated_at': updated_at}))

async def process_json_file(async_client, file_path, data, network, semaphore, delay_time, system_prompt, model):
    async with semaphore:
        print(f'Analyzing: {file_path}...')
        explanation = ""
        async for item in explain_transaction(async_client, data, network=network, system_prompt=system_prompt, model=model):
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
    api_key_groq = os.getenv('GROQ_API_KEY')
    groq_client = AsyncGroq(api_key=api_key_groq)
    semaphore = asyncio.Semaphore(max_concurrent_connections)
    
    tasks = []
    models={
        "llama3-70b-8192":"groq",
        "llama3-8b-8192":"groq",
        "mixtral-8x7b-32768":"groq",
        "gemma-7b-it":"groq",
        "claude-3-haiku-20240307":"anthropic",
        "claude-3-opus-20240229":"anthropic",
        "claude-3-sonnet-20240229":"anthropic",
    }
    for file_path, data in json_data:
        if models[model]=="groq":
            task = asyncio.create_task(process_json_file(groq_client, file_path, data, network, semaphore, delay_time, system_prompt, model))
        elif models[model]=="anthropic":
            task = asyncio.create_task(process_json_file(anthropic_client, file_path, data, network, semaphore, delay_time, system_prompt, model))
        else:
            #Assume there is new model that was not added to models list, use Anthropic client by default
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