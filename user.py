import os
import json
import asyncio
import logging
from google.cloud import storage
from datetime import datetime, timedelta
from dotenv import load_dotenv
from eth_account.messages import encode_defunct
from web3 import Web3




load_dotenv()

logging.getLogger().setLevel(logging.INFO)
bucket_name = os.getenv('GCS_BUCKET_NAME')
storage_client = storage.Client()
bucket = storage_client.bucket(bucket_name)


async def verify_signature(signature, user_account):
    w3 = Web3(Web3.HTTPProvider('https://eth.llamarpc.com'))
    mesage= encode_defunct(text="I am the owner of this address and want to sign in to tx-explan:"+str(user_account))
    address = w3.eth.account.recover_message(mesage,signature=signature)
    return address.lower() == user_account.lower()

async def post_user_feedback(input_json,user):
    user_account=user
    data=input_json
    print (data)
    try:
        transaction_hash=data["hash"]
        print (transaction_hash)
        file_path=f'users/{user}/feedback/{transaction_hash}.json'
        blob = bucket.blob(file_path)
        blob.upload_from_string(json.dumps(data, indent = 4))
        return ("OK")
    except Exception as e:
        print (e)
        return(e)

async def count_user_feedback(user):
    user_account=user
    folder_path=f'users/{user}/feedback/'
    blobs = list(bucket.list_blobs(prefix=folder_path))
    feedback_count=len(blobs)
    return feedback_count