# TX Explain

TX Explain is a Python-based project that allows you to simulate and analyze blockchain transactions using the Tenderly API and the Anthropic API. It supports multiple blockchain networks, including Ethereum, Arbitrum, Avalanche, and Optimism.

## Features

- Simulate blockchain transactions for a specified date range and network using the Tenderly API
- Analyze simulated transaction data using the Anthropic API to extract insights and generate explanations
- Customize the analysis by providing an optional system prompt
- Configure various parameters such as delay time between API requests, maximum concurrent connections, and function calls to skip
- Store simulated transaction data and analysis results in a Google Cloud Storage bucket

## Prerequisites

Before using the TX Explain, ensure that you have the following:

- Python 3.x installed
- Google Cloud Storage account and credentials
- Tenderly API access (account slug, project slug, and access key)
- Anthropic API access key

## Setup

1. Clone the repository:

```
git clone https://github.com/your-username/tx-explain.git
```

2. Change into the project directory:

```
cd tx-explain
```

3. Install the required Python dependencies:

```
pip install -r requirements.txt
```

4. Create a `.env` file in the project root and add your API keys and configuration settings:

```
ANTHROPIC_API_KEY=your-anthropic-api-key
TENDERLY_ACCOUNT_SLUG=your-tenderly-account-slug
TENDERLY_PROJECT_SLUG=your-tenderly-project-slug
TENDERLY_ACCESS_KEY=your-tenderly-access-key
```

5. Authenticate Google Cloud:

   a. Install the Google Cloud SDK by following the instructions for your operating system: [Google Cloud SDK Installation Guide](https://cloud.google.com/sdk/docs/install)

   b. Initialize the Google Cloud SDK by running the following command and following the prompts:

   ```
   gcloud init
   ```

   c. Authenticate your Google Cloud account by running the following command:

   ```
   gcloud auth application-default login
   ```

   This command will open a browser window where you can sign in to your Google Cloud account and grant the necessary permissions.

   d. Set the `GOOGLE_APPLICATION_CREDENTIALS` environment variable to the path of your Google Cloud credentials JSON file:

   ```
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/your/credentials.json
   ```

   Replace `/path/to/your/credentials.json` with the actual path to your Google Cloud credentials JSON file.

6. (Optional) If you want to use a custom system prompt for the Anthropic API, edit the `system_prompt.txt` file in the project root with your prompt text and pass in `-p system_prompt.txt` when you run the script.

## Usage

To run the TX Explain, use the `main.py` script with the desired command-line arguments:

```
python main.py -n <network> -s <start_date> -e <end_date> -d <delay_time> -c <max_concurrent_connections> -f <skip_functions> -p <system_prompt_file>
```

- `-n` or `--network`: The blockchain network to simulate and analyze transactions for (default: ethereum)
- `-s` or `--start`: The start date for transaction simulation (default: yesterday)
- `-e` or `--end`: The end date for transaction simulation (default: today)
- `-d` or `--delay`: The delay time between API requests in seconds (default: 1.2)
- `-c` or `--concurrency`: The maximum number of concurrent connections to the API (default: 1)
- `-f` or `--skip-functions`: The list of function calls to skip (default: transfer approve transferFrom)
- `-p` or `--prompt`: The path to the file containing the system prompt (default: None)

Example:

```
python main.py -n ethereum -s 2023-05-01 -e 2023-05-02 -d 1.5 -c 2 -f transfer approve -p system_prompt.txt
```

The script will first run `simulate.py` to simulate transactions for the specified network and date range, saving the results to a Google Cloud Storage bucket. Then, it will run `explain.py` to analyze the simulated transaction data using the Anthropic API, saving the analysis results back to the bucket.

## Project Structure

The project has the following structure:

```
tx-explain/
│
├── main.py
│
├── simulate.py
│
├── explain.py
│
├── system_prompt.txt
│
└── .env
```

- `main.py`: The main script that orchestrates the execution of `simulate.py` and `explain.py`.
- `simulate.py`: The script responsible for simulating blockchain transactions using the Tenderly API.
- `explain.py`: The script responsible for analyzing simulated transaction data using the Anthropic API.
- `system_prompt.txt`: (Optional) The file containing the custom system prompt for the Anthropic API.
- `.env`: The file containing environment variables and configuration settings.

## Contributing

Contributions to the TX Explain project are welcome! If you find any issues or have suggestions for improvements, please open an issue or submit a pull request on the GitHub repository.

## License

This project is licensed under the [MIT License](LICENSE).
```

This README provides an overview of the TX Explain project, including its features, prerequisites, setup instructions, usage guidelines, project structure, and contributing information. It serves as a comprehensive guide for users to understand and get started with the project.

Feel free to modify and enhance the README based on your specific project requirements and additional details you want to include.