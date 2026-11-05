import json
import requests
import time
import random
import re
import csv
import os
import argparse
import itertools
import signal
from datetime import datetime
from faker import Faker
from dotenv import load_dotenv

from db import SubmissionDB

load_dotenv()

class SibFormsReplayer:
    def __init__(self, har_file_path, db=None, csv_path=None):
        self.har_file_path = har_file_path
        self.db = db
        self.success_count = 0
        self.failure_count = 0
        self.total_attempts = 0
        self.last_status_code = None

        # Optional CSV logging (off unless --csv is passed)
        self.csv_path = csv_path
        self._csv_file = None
        if self.csv_path:
            self._csv_fields = [
                "timestamp", "index", "email", "first_name", "last_name",
                "success", "message"
            ]
            write_header = (not os.path.exists(self.csv_path)) or os.path.getsize(self.csv_path) == 0
            self._csv_file = open(self.csv_path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=self._csv_fields)
            if write_header:
                self._csv_writer.writeheader()
                self._csv_file.flush()

        # Load HAR data
        with open(self.har_file_path, 'r', encoding='utf-8') as f:
            self.har_data = json.load(f)

        # Extract the specific POST request to sibforms.com
        self.entry = None
        for entry in self.har_data['log']['entries']:
            if 'sibforms.com' in entry['request']['url'] and entry['request']['method'] == 'POST':
                self.entry = entry
                break

        if not self.entry:
            raise ValueError("No POST request to sibforms.com found in the HAR file.")

        # Initialize Faker with South African locale for realistic names
        try:
            self.faker = Faker("za_ZA")
        except Exception:
            self.faker = Faker("en_US") 
        # self.faker.seed(42)  # Uncomment for reproducible results during testing

    def parse_request(self):
        """Parse the HAR entry to extract URL, Headers, and Boundary."""
        req = self.entry['request']

        # 1. Extract URL
        full_url = req['url']

        # 2. Build headers dict for easy access
        headers = {h['name']: h['value'] for h in req['headers']}

        # 3. Dynamically find Content-Type header (fixes hardcoded index bug)
        content_type = None
        for h in req['headers']:
            if h['name'].lower() == 'content-type':
                content_type = h['value']
                break

        if not content_type:
            raise ValueError("No Content-Type header found in the HAR request.")

        # 4. Extract boundary safely
        match = re.search(r'boundary="?([^";\s]+)"?', content_type)
        if not match:
            raise ValueError("Could not find boundary in Content-Type header.")

        boundary = match.group(1)

        return {
            'url': full_url,
            'headers': headers,
            'boundary': boundary
        }

    def generate_realistic_entry(self):
        """Generate a realistic name and corresponding email."""
        first_name = self.faker.first_name()
        last_name = self.faker.last_name()

        # Clean names for email format (remove spaces, hyphens, apostrophes, lowercase)
        clean_first = re.sub(r"[\s'\-]", "", first_name).lower()
        clean_last = re.sub(r"[\s'\-]", "", last_name).lower()

        # Format: <name>.<surname>@stitchs.co.za
        email = f"{clean_first}.{clean_last}@stitchs.co.za"

        return email, first_name, last_name

    def reconstruct_body(self, email, first_name, boundary):
        """Reconstruct the multipart body with the new email and first name."""
        body_parts = []

        def add_field(name, value):
            part = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            body_parts.append(part)

        add_field("EMAIL", email)
        add_field("FIRSTNAME", first_name)
        add_field("BIRTHDAY", "")
        add_field("email_address_check", "")
        add_field("locale", "en")

        # Closing boundary
        body_parts.append(f"--{boundary}--\r\n")
        return "".join(body_parts)

    def send_request(self, email, first_name):
        """Send the POST request."""
        # Reset last status for this attempt (None if no response received)
        self.last_status_code = None
        parsed = self.parse_request()

        # Reconstruct body with new email and name
        new_body = self.reconstruct_body(email, first_name, parsed['boundary'])

        # Update Content-Length to match new payload size
        content_length = len(new_body.encode('utf-8'))
        parsed['headers']['Content-Length'] = str(content_length)

        try:
            response = requests.post(
                parsed['url'],
                headers=parsed['headers'],
                data=new_body.encode('utf-8'),
                timeout=10
            )

            self.last_status_code = response.status_code

            if response.status_code == 200:
                try:
                    json_resp = response.json()
                    if json_resp.get('success') is True:
                        self.success_count += 1
                        return True, f"Success: {json_resp.get('message', '')}"
                    else:
                        self.failure_count += 1
                        return False, f"API success=False: {json_resp}"
                except ValueError:
                    self.failure_count += 1
                    return False, f"HTTP 200 but invalid JSON: {response.text[:100]}"
            else:
                self.failure_count += 1
                return False, f"HTTP {response.status_code}: {response.text[:100]}"

        except Exception as e:
            self.failure_count += 1
            return False, f"Exception: {str(e)}"

    def log_to_csv(self, index, email, first_name, last_name, success, message):
        """Append an entry (success or failure) to the CSV file."""
        if not self._csv_file:
            return
        self._csv_writer.writerow({
            "timestamp": datetime.now().isoformat(),
            "index": index,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "success": success,
            "message": message,
        })
        self._csv_file.flush()

    def close_csv(self):
        """Close the CSV file handle."""
        if not self._csv_file:
            return
        try:
            self._csv_file.close()
        except Exception:
            pass

    def run(self, num_entries=None):
        """Run the replay loop. num_entries=None runs indefinitely."""
        label = "infinite" if num_entries is None else num_entries
        print(f"Starting SibForms Replay for {label} entries...")
        print("-" * 40)

        # None -> count up forever; otherwise a bounded range
        counter = itertools.count(1) if num_entries is None else range(1, num_entries + 1)
        total_label = "inf" if num_entries is None else num_entries

        try:
            for i in counter:
                email, first_name, last_name = self.generate_realistic_entry()
                success, message = self.send_request(email, first_name)

                status_icon = "✅" if success else "❌"
                print(f"[{i}/{total_label}] {status_icon} {email} | {first_name} {last_name} | {message}")

                # Write every entry (success and failure) to Neon / CSV
                if self.db:
                    self.db.record(email, first_name, last_name, success, self.last_status_code, message)
                self.log_to_csv(i, email, first_name, last_name, success, message)

                # On a 429 (rate limited), back off for 30s before continuing
                if self.last_status_code == 429:
                    print("   ⏳ HTTP 429 received — sleeping 30s before continuing...")
                    time.sleep(30)

                # Human-like delay to avoid rate limits
                time.sleep(random.uniform(0.5, 1.5))
        except KeyboardInterrupt:
            print("\nInterrupted by user (Ctrl+C).")

        self.total_attempts = self.success_count + self.failure_count
        print("-" * 40)
        print("Results:")
        print(f"Total Attempts: {self.total_attempts}")
        if self.total_attempts > 0:
            print(f"Successes:      {self.success_count} ({(self.success_count/self.total_attempts*100):.2f}%)")
            print(f"Failures:       {self.failure_count} ({(self.failure_count/self.total_attempts*100):.2f}%)")
        else:
            print("No attempts made.")

        self.close_csv()
        if self.csv_path:
            print(f"Results written to: {self.csv_path}")

        if self.db:
            row = self.db.stats()
            if row:
                total, ok, bad, limited, pct = row
                print(f"Lifetime DB stats [{self.db.run_label}]: {total} total, "
                      f"{ok} ok, {bad} failed, {limited} rate-limited, {pct}% success")
            self.db.close()

def parse_count(value):
    """Parse --count into None (infinite) or a positive int."""
    if str(value).lower() in ("inf", "infinity", "infinite"):
        return None
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid count: {value!r} (use an integer or 'inf')")
    if n < 0:
        raise argparse.ArgumentTypeError(f"count must be >= 0 (got {n})")
    return n

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SibForms replayer.")
    parser.add_argument(
        "-n", "--count",
        type=parse_count,
        default=None,
        help="Number of entries to run. Use an integer, or 'inf' for infinite (default: inf).",
    )
    parser.add_argument("--run-label", default="default",
                        help="Name to group stats under in the DB (default: 'default').")
    parser.add_argument("--max-rows", type=int, default=10_000,
                        help="Keep at most this many submission rows in the DB (default: 10000).")
    parser.add_argument("--csv", metavar="PATH", default=None,
                        help="Also append results to this CSV file.")
    parser.add_argument("--no-db", action="store_true",
                        help="Don't write to the database.")
    parser.add_argument("--serve", action="store_true",
                        help="Run a status HTTP server on $PORT and self-ping to stay awake (Render free tier).")
    args = parser.parse_args()

    num_entries = args.count

    # Render (and most hosts) stop processes with SIGTERM; treat it like Ctrl+C
    # so the run loop exits cleanly, prints stats and closes the DB connection.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)

    har_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'submit.har')
    try:
        db = None if args.no_db else SubmissionDB(run_label=args.run_label, max_rows=args.max_rows)
        replayer = SibFormsReplayer(har_file, db=db, csv_path=args.csv)

        if args.serve:
            from keepalive import start_http_server, start_self_ping
            start_http_server(replayer, int(os.environ.get("PORT", "10000")))
            # Render sets RENDER_EXTERNAL_URL on web services automatically
            public_url = os.environ.get("RENDER_EXTERNAL_URL")
            if public_url:
                start_self_ping(public_url)
            else:
                print("RENDER_EXTERNAL_URL not set; skipping self-ping.")

        replayer.run(num_entries=num_entries)
    except FileNotFoundError:
        print(f"Error: File '{har_file}' not found. Please place the HAR file in the same directory or update the path.")
    except Exception as e:
        print(f"Fatal Error: {e}")