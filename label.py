import json
import re
import pandas as pd

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
def format (addresses):
    addresses_str = ', '.join([f"'{address}'" for address in addresses if address])
    return addresses_str


# Query labels datatables
def query_labels (addresses_str, labels_dataset, ens_dataset, client):
    try:
        sql = f"""
            with labels_data as (
                select address,
                       address_name
                from {labels_dataset}
                where address in ({addresses_str})
                ),
            ens_data as (
                select address,
                       tag as address_name
                from {ens_dataset}
                where address in ({addresses_str})
                  and address not in (select address from labels_data)
                ),
            merged_data as (
                select * from labels_data
                union all 
                select * from ens_data 
            )

            select * from merged_data
        """
        df = client.query(sql).result().to_dataframe()
        return df
    except Exception as e:
        print("Error at query_labels: ", e)
        return pd.DataFrame()

# Fetch address labels
def fetch_address_labels(sim_data, labels_dataset, ens_dataset, client):
    address_regex = r'^0x[0-9a-fA-F]{40}$'
    try:
        addresses = extract(sim_data, address_regex)
        addresses_str = format(addresses)

        labels = query_labels (addresses_str, labels_dataset, ens_dataset, client)

        labels_json = labels.to_json(orient='records')
        labels_json = '{"address_labels": ' + labels_json + '}'

        return labels_json
    except Exception as e:
        print("Error at fetch_address_labels: ", e)

# Add address labels to the original sim_data
def add_labels(sim_data, labels_dataset, ens_dataset, client):
    try:
        labels_json = json.loads(fetch_address_labels(sim_data, labels_dataset, ens_dataset, client))
        for key, value in labels_json.items():
            sim_data[key] = value

        return sim_data
    except Exception as e:
        print("Error at add_labels: ", e)

