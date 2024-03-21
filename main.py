import argparse
import subprocess

def run_script(script_name, args):
    command = ['python', script_name] + args
    subprocess.run(command, check=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Blockchain Transaction Simulator and Analyzer')
    parser.add_argument('-n', '--network', type=str, default='ethereum', choices=['ethereum', 'arbitrum', 'avalanche', 'optimism'],
                        help='Blockchain network to simulate and analyze transactions for (default: ethereum)')
    parser.add_argument('-s', '--start', type=str, default=None,
                        help='Start day for transaction simulation (default: yesterday)')
    parser.add_argument('-e', '--end', type=str, default=None,
                        help='End day for transaction simulation (default: today)')
    parser.add_argument('-d', '--delay', type=float, default=1.2,
                        help='Delay time between API requests in seconds (default: 1.2)')
    parser.add_argument('-c', '--concurrency', type=int, default=1,
                        help='Maximum number of concurrent connections to the API (default: 1)')
    parser.add_argument('-f', '--skip-functions', type=str, nargs='+', default=['transfer', 'approve', 'transferFrom'],
                        help='List of function calls to skip (default: transfer approve transferFrom)')
    parser.add_argument('-p', '--prompt', type=str, default=None,
                        help='Path to the file containing the system prompt (default: None)')
    args = parser.parse_args()

    network = args.network
    start_day = args.start
    end_day = args.end
    delay_time = args.delay
    max_concurrent_connections = args.concurrency
    skip_function_calls = args.skip_functions
    system_prompt_file = args.prompt

    # Run simulate.py
    simulate_args = ['-n', network]
    if start_day:
        simulate_args.extend(['-s', start_day])
    if end_day:
        simulate_args.extend(['-e', end_day])
    run_script('simulate.py', simulate_args)

    # Run explain.py
    explain_args = ['-n', network, '-d', str(delay_time), '-c', str(max_concurrent_connections)]
    explain_args.extend(['-s'] + skip_function_calls)
    if system_prompt_file:
        explain_args.extend(['-p', system_prompt_file])
    run_script('explain.py', explain_args)