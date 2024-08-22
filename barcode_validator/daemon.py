import argparse
import requests
import time
import os
import subprocess
import logging
import sqlite3
import traceback
import base64
from datetime import datetime, timedelta
from config import Config
from barcode_validator.core import BarcodeValidator

GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
POLL_INTERVAL = 300  # 5 minutes

headers = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json'
}


def setup_database(config):
    conn = sqlite3.connect(config.get('pr_db_file'))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS prs
                 (pr_number INTEGER PRIMARY KEY, status TEXT, last_updated TIMESTAMP)''')
    conn.commit()
    return conn


def get_open_prs(config):
    """
    Get a list of open pull requests for the repository from the remote location on GitHub via its API. The location
    of the pull requests endpoint is constructed from information held by the Config object.
    :return: A list of pull request
    """
    url = f'https://api.github.com/repos/{config.get("repo_owner")}/{config.get("repo_name")}/pulls'
    response = requests.get(url, headers=headers)
    return response.json()


def get_pr_files(config, pr_number):
    """
    Get a list of files for a given pull request. The location of the files-for-this-pull-request endpoint is
    constructed from the repository owner and name (which are held by the Config object) as well as the pull request
    number, which is passed as an argument.
    :param config: The Config object
    :param pr_number: The pull request number
    :return: A list of file objects
    """
    url = f'https://api.github.com/repos/{config.get("repo_owner")}/{config.get("repo_name")}/pulls/{pr_number}/files'
    response = requests.get(url, headers=headers)
    return response.json()


def run_validation(config, pr_number, branch, validator):
    """
    Run the validation process for a given pull request.
    :param config: The Config object
    :param pr_number: The pull request number
    :param branch: The branch name
    :param validator: A BarcodeValidator object
    :return: A list of validation results
    """

    # Go to local clone and fetch the PR
    repo_location = config.get('repo_location')
    os.chdir(repo_location)
    subprocess.run(['git', 'fetch', 'origin', f'{branch}:pr-{pr_number}'])
    subprocess.run(['git', 'checkout', f'pr-{pr_number}'])

    # Get the FASTA files from the PR
    files = get_pr_files(config, pr_number)
    fasta_files = [f for f in files if f['filename'].endswith(('.fasta', '.fa', '.fas'))]

    # Run the validation process for each FASTA file
    all_results = []
    for file in fasta_files:

        # Fetch file content
        file_url = file['raw_url']
        response = requests.get(file_url, headers=headers)
        if response.status_code == 200:

            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(file['filename']), exist_ok=True)

            # Save file content
            with open(file['filename'], 'wb') as f:
                f.write(response.content)

            # Validate file
            logging.info(f"Validating {file['filename']}...")
            results = validator.validate_fasta(file['filename'])
            logging.info(f"Validation complete for {file['filename']}")
            all_results.extend([(file['filename'], result) for result in results])
        else:
            logging.error(f"Failed to fetch file {file['filename']}: {response.status_code}")

    return all_results


def post_comment(config, pr_number, results):
    """
    Post a comment to a pull request with the validation results.
    :param config: The Config object
    :param pr_number: The pull request number
    :param results: A list of validation results
    :return: None
    """
    url = f'https://api.github.com/repos/{config.get("repo_owner")}/{config.get("repo_name")}/issues/{pr_number}/comments'
    comment = "## Validation Results\n\n"
    for file, result in results:
        comment += f"### {file}\n"
        barcode_rank, full_rank, messages = result.calculate_ranks(verbosity=3)
        comment += f"Barcode Rank: {barcode_rank}\n"
        comment += f"Full Rank: {full_rank}\n"
        comment += f"Messages:\n{messages}\n\n"

    payload = {'body': comment}
    requests.post(url, headers=headers, json=payload)


def process_pr(config, validator, conn, pr_number, branch):
    c = conn.cursor()
    c.execute("SELECT status FROM prs WHERE pr_number = ?", (pr_number,))
    row = c.fetchone()

    if row is None or row[0] == 'pending':
        try:
            c.execute("INSERT OR REPLACE INTO prs (pr_number, status, last_updated) VALUES (?, 'processing', ?)",
                      (pr_number, datetime.now()))
            conn.commit()

            results = run_validation(config, pr_number, branch, validator)
            post_comment(config, pr_number, results)

            c.execute("UPDATE prs SET status = 'completed', last_updated = ? WHERE pr_number = ?",
                      (datetime.now(), pr_number))
            conn.commit()
        except Exception as e:
            error_msg = f"Error processing PR {pr_number}: {str(e)}\n"
            error_msg += "Stack trace:\n"
            error_msg += traceback.format_exc()
            logging.error(error_msg)
            c.execute("UPDATE prs SET status = 'error', last_updated = ? WHERE pr_number = ?",
                      (datetime.now(), pr_number))
            conn.commit()


def main(config_file, verbosity):
    """
    Main function for the barcode validator daemon.
    :param config_file: Path to the configuration file
    :param verbosity: Logging verbosity level
    :return: None
    """

    # Initialize the Config object and setup logging
    config = Config()
    config.load_config(config_file)
    config.setup_logging(verbosity)

    # Set up the database connection for tracking PRs
    try:
        logging.info(f"Going to initialize database at {config.get('pr_db_file')}")
        conn = setup_database(config)
        logging.info("Database initialized")
    except Exception as e:
        logging.error(f"Error setting up database: {str(e)}")
        exit(1)

    # Initialize the BarcodeValidator object
    logging.info("Initializing BarcodeValidator...")
    validator = BarcodeValidator(config)
    validator.initialize()

    # Start the main loop
    logging.info("Starting main loop")
    while True:

        # Get a list of open PRs and iterate over them
        logging.info("Checking for open PRs...")
        prs = get_open_prs(config)
        logging.info(f"Found {len(prs)} open PRs")

        for pr in prs:

            # Process PRs that contain FASTA files
            pr_number = pr['number']
            logging.info(f"Inspecting files from PR {pr['number']}...")
            files = get_pr_files(config, pr_number)
            logging.info(f"Found {len(files)} files in PR {pr['number']}")

            # Iterate over the files in the PR and process if any are FASTA files
            if any(f['filename'].endswith(('.fasta', '.fa', '.fas')) for f in files):
                process_pr(config, validator, conn, pr_number, pr['head']['ref'])

        # Clean up old completed PRs
#        c = conn.cursor()
#        c.execute("DELETE FROM prs WHERE status = 'completed' AND last_updated < ?",
#                  (datetime.now() - timedelta(days=7),))
#        conn.commit()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GitHub PR Validation Daemon for barcode_validator")
    parser.add_argument("-c", "--config_file", required=True, help="Path to the configuration YAML file")
    parser.add_argument("-v", "--verbosity", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        default='INFO', help="Set the logging verbosity (default: INFO)")
    args = parser.parse_args()

    main(args.config_file, args.verbosity)