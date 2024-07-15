import json
import re
import os
from dotenv import load_dotenv
from flipside import Flipside
import pandas as pd
import aiohttp
import asyncio

load_dotenv()

flipside_api_key = os.getenv('FLIPSIDE_API_KEY')
flipside_endpoint_url = os.getenv('FLIPSIDE_ENDPOINT_URL')
flipside = Flipside(flipside_api_key, flipside_endpoint_url)

# Recursively iterate over the json object looking for specified pattern
def explore_json(obj, items, pattern):
    try:
        if isinstance(obj, dict):
            for value in obj.values():
                if isinstance(value, str) and re.match(pattern, value):
                    items.append(value)
                explore_json(value, items, pattern)
        elif isinstance(obj, list):
            for item in obj:
                explore_json(item, items, pattern)
        else:
            pass
    
    except Exception as e:
        print("explore_json error: ", e)


# Extract the unique occurences of the specified pattern
def extract(json_obj, pattern = None):
    try:
        items = []
        explore_json(json_obj, items, pattern)
        unique_items = list(set(items))
        return unique_items
    except Exception as e:
        print("extract error: ", e)


# Make the addresses into a string that will be used in the flipside query
def addresses_to_string (addresses):
    try:
        addresses_str = ', '.join([f"'{address}'" for address in addresses if address])
        return addresses_str
    except Exception as e:
        print("Error at format: ", e)
        return None


# Query Flipside
def query_flipside (addresses_str, endpoint, network):
    try:
        if network == 'ethereum':
            sql = f"""
                select address,
                    address_name,
                    label,
                    label_type,
                    label_subtype
                from ethereum.core.dim_labels 
                where lower(address) in ({addresses_str})
                """
        else:
            sql = f"""
                select address,
                    address_name,
                    project_name as label,
                    label_type,
                    label_subtype
                from {network}.core.dim_labels 
                where lower(address) in ({addresses_str})
                """
        query_result_set = endpoint.query(sql)
        df = pd.DataFrame(query_result_set)
        return df
    except Exception as e:
        print("Error at query_flipside: ", e)
        return None
    
# Async version of labels query for explain_account
async def async_query_flipside(addresses_str, network, endpoint = flipside):
    try:
        if network == 'ethereum':
            sql = f"""
                select address,
                    address_name,
                    label,
                    label_type,
                    label_subtype
                from ethereum.core.dim_labels 
                where lower(address) in ({addresses_str})
                """
        else:
            sql = f"""
                select address,
                    address_name,
                    project_name as label,
                    label_type,
                    label_subtype
                from {network}.core.dim_labels 
                where lower(address) in ({addresses_str})
                """
        
        # Execute the query asynchronously
        loop = asyncio.get_event_loop()
        query_result_set = await loop.run_in_executor(None, endpoint.query, sql)
        
        df = pd.DataFrame(query_result_set)
        return df
    except Exception as e:
        print("Error at query_flipside: ", e)
        return None

# Put results into a json object
def to_json(df):
    try:
        json_data = []
        data = df[1][4]
        if data is None:
            print("Data is None, returning an empty JSON object")
            return json.dumps({'address_labels': []}, indent=4)

        for index, item in enumerate(data):
            json_data.append({
                'address': item[0],
                'address_name': item[1],
                'label': item[2],
                'label_type': item[3],
                'label_subtype': item[4]
            })
    
        json_object = json.dumps({'address_labels': json_data}, indent=4)
        return json_object
    except Exception as e:
        print("Error at to_json: ", e)

# Fetch address labels
def fetch_address_labels(sim_data, endpoint, network):
    address_regex = r'^0x[0-9a-fA-F]{40}$'
    try:
        addresses = extract(sim_data, address_regex)
        addresses_str = addresses_to_string(addresses)
        labels_data = query_flipside(addresses_str,endpoint, network)
        labels_json = to_json(labels_data)
        return labels_json
    except Exception as e:
        print("Error at fetch_address_labels: ", e)

# Add Ethereum labels through bigquery
async def add_labels(sim_data, labels_dataset, bigquery_client):
    address_regex = r'^0x[0-9a-fA-F]{40}$'
    try:
        addresses = extract(sim_data, address_regex)
        addresses_str = addresses_to_string(addresses)
        sql = f"""select *
                  from {labels_dataset}
                  where lower(address) in ({addresses_str})
                """
        query_result_set = bigquery_client.query(sql)
        results = query_result_set.result()
        rows = [dict(row) for row in results]
        sim_data["address_labels"] = rows
        return sim_data
    except Exception as e:
        print("Error at add_labels: ", e)