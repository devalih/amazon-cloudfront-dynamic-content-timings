
import pycurl
import argparse
import sys
import threading
import time
from tabulate import tabulate
from queue import Queue
from concurrent.futures import ThreadPoolExecutor

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument("-url", type=str, required=True, help='URL to test')
arg_parser.add_argument("-n", type=int, nargs='?', help='Number of requests')
arg_parser.add_argument("-c", type=int, nargs='?', help='Concurrency level (number of parallel requests)')
arg_parser.add_argument("-v", "--verbose", action='store_true', help='Shows the detailed table of requests')
args = arg_parser.parse_args()

if not args.n:
    requests_total = 100
else:
    requests_total = args.n

if not args.c:
    concurrency = 1  # Default to sequential execution
else:
    concurrency = min(args.c, requests_total)  # Can't have more concurrent requests than total

try:
    from io import BytesIO
except ImportError:
    from StringIO import StringIO as BytesIO

# Create a lock for thread-safe access to shared variables
result_lock = threading.Lock()

# Shared counters
reused_conns_count = 0
reused_conns_download_times = 0
new_conns_download_times = 0

# Results list to store the outcomes of all requests
results = []

# URL to test
url = args.url

# Progress tracking
completed_requests = 0

def header_function(header_line, headers):
    """Parse header line and add to headers dictionary"""
    header_line = header_line.decode('iso-8859-1')

    if ':' not in header_line:
        return

    name, value = header_line.split(':', 1)
    name = name.strip()
    value = value.strip()
    name = name.lower()
    headers[name] = value

def perform_request(request_id):
    """Function to perform a single request and measure performance metrics"""
    global reused_conns_count, reused_conns_download_times, new_conns_download_times, completed_requests
    
    # Initialize buffer and headers dictionary for this request
    buffer = BytesIO()
    headers = {}
    
    # Set up the pycurl request
    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.WRITEFUNCTION, buffer.write)
    c.setopt(c.HEADERFUNCTION, lambda x: header_function(x, headers))
    c.setopt(c.FOLLOWLOCATION, 1)  # Follow redirects
    
    # Perform the request
    try:
        c.perform()
        status_code = c.getinfo(pycurl.RESPONSE_CODE)
    except Exception as e:
        print(f"Error on request {request_id+1}: {e}")
        status_code = 0  # Error code
    
    # Extract timing metrics
    total_time = round(c.getinfo(pycurl.TOTAL_TIME) * 1000, 1)
    downstream_connect_time = round(c.getinfo(pycurl.CONNECT_TIME) * 1000, 1)
    namelookup_time = round(c.getinfo(pycurl.NAMELOOKUP_TIME) * 1000, 1)
    appconnect_time = round(c.getinfo(pycurl.APPCONNECT_TIME) * 1000, 1)
    starttransfer_time = round(c.getinfo(pycurl.PRETRANSFER_TIME) * 1000, 1)
    download_speed = round(c.getinfo(pycurl.SPEED_DOWNLOAD) /125000, 1) #Mbps
    
    # Get page size in KB
    page_size_kb = round(len(buffer.getvalue()) / 1024, 1)  # Convert bytes to KB
    
    # Initialize variables for CloudFront-specific metrics
    upstream_connect_time = 0
    origin_fbl = 0
    cf_fbl = 0
    
    # Process server timing headers if present
    server_timing_present = False
    if 'server-timing' in headers:
        server_timing_present = True
        timings_list = headers['server-timing'].split(',')
        for nr, item in enumerate(timings_list):
            if item == 'cdn-cache-miss' or item == 'cdn-cache-hit' or item == 'cdn-cache-refresh':
                timings_list[nr] = item + "=" + ';desc="true"'
                
        try:
            timings_dict = dict(s.split(';') for s in timings_list)
            for key, value in timings_dict.items():
                if key == 'cdn-downstream-fbl' or key == 'cdn-upstream-dns' or key == 'cdn-upstream-connect' or key == 'cdn-upstream-fbl':
                    value = float(int(value.replace("dur=", "").replace("desc=", "")))
                    if key == 'cdn-upstream-connect':
                        upstream_connect_time = value
                        with result_lock:
                            if value == 0:
                                reused_conns_count += 1
                                reused_conns_download_times += total_time
                            else:
                                new_conns_download_times += total_time
                    elif key == 'cdn-upstream-fbl':
                        origin_fbl = value
                    elif key == 'cdn-downstream-fbl':
                        cf_fbl = value
        except Exception as e:
            print(f"Error parsing server timing headers: {e}")
    
    # Create result record
    result = [request_id+1, status_code, total_time, namelookup_time, downstream_connect_time, 
              appconnect_time, starttransfer_time, upstream_connect_time, 
              origin_fbl, cf_fbl, download_speed, page_size_kb]
    
    # Thread-safe addition to results list and update progress
    with result_lock:
        results.append(result)
        global completed_requests
        completed_requests += 1
        # Show progress
        progress = (completed_requests / requests_total) * 100
        bar_length = 30
        filled_length = int(bar_length * completed_requests // requests_total)
        bar = '█' * filled_length + '░' * (bar_length - filled_length)

        # Use carriage return to update the same line
        sys.stdout.write(f"\rProgress: [{bar}] {completed_requests}/{requests_total} requests ({progress:.1f}%)")
        sys.stdout.flush()
    
    # Clean up
    c.close()
    
    return result

# Add main execution section with thread pool
print(f"Running {requests_total} requests with concurrency level {concurrency}")
start_time = time.time()

# Using ThreadPoolExecutor to manage the thread pool
with ThreadPoolExecutor(max_workers=concurrency) as executor:
    # Submit all requests to the executor
    future_to_id = {executor.submit(perform_request, i): i for i in range(requests_total)}

# Move to a new line after progress bar
if not args.verbose:
    print("\n")

# Calculate total execution time
total_execution_time = time.time() - start_time

# Sort results by request ID to maintain order in output
results.sort(key=lambda x: x[0])

# Calculate statistics and print results if verbose mode is enabled
if args.verbose:
    print(tabulate(
        results,
        tablefmt='grid',
        headers=["Request number", "Status", "Download time", "DNS resolution", "Downstream connect time", "Downstream TCP+SSL time", "User FBL", "Upstream TCP+SSL time", "Origin FBL", "CF FBL", "Download speed, Mbps", "Size (KB)"]
        )
    )

# Calculate averages
reused_conns_download_times_avg = 'NA' if reused_conns_count == 0 else round(reused_conns_download_times / reused_conns_count)
new_conns_download_times_avg = 'NA' if requests_total - reused_conns_count == 0 else round(new_conns_download_times / (requests_total - reused_conns_count))
latency_gain = 'NA' if new_conns_download_times_avg == 'NA' or reused_conns_download_times_avg == 'NA' else round(100 - reused_conns_download_times_avg / new_conns_download_times_avg * 100, 1)

# Calculate performance metrics
avg_download_time = sum(row[2] for row in results) / len(results)  # Index 2 is download time now
min_download_time = min(row[2] for row in results)
max_download_time = max(row[2] for row in results)

# Calculate page size metrics
total_kb = sum(row[11] for row in results)  # Index 11 is page size in KB
avg_kb = total_kb / len(results)
min_kb = min(row[11] for row in results)
max_kb = max(row[11] for row in results)

# Count status codes by category
status_codes = {}
status_2xx = 0
status_3xx = 0
status_4xx = 0
status_5xx = 0
status_other = 0

for row in results:
    status = row[1]  # Status code is at index 1
    if status not in status_codes:
        status_codes[status] = 0
    status_codes[status] += 1
    
    # Categorize status codes
    if 200 <= status < 300:
        status_2xx += 1
    elif 300 <= status < 400:
        status_3xx += 1
    elif 400 <= status < 500:
        status_4xx += 1
    elif 500 <= status < 600:
        status_5xx += 1
    else:
        status_other += 1

# Print summary statistics
print(f"\nExecution Summary:")
print(f"Total execution time: {round(total_execution_time, 2)} seconds")
print(f"Requests per second: {round(requests_total / total_execution_time, 2)}")
print(f"Mean Time per request: {round(avg_download_time, 1)} ms")
print(f"Min/Avg/Max download time: {min_download_time}/{round(avg_download_time, 1)}/{max_download_time} ms")
print(f"Total size: {round(total_kb, 1)} KB")
print(f"Min/Avg/Max page size: {min_kb}/{round(avg_kb, 1)}/{max_kb} KB")

print("\nStatus Code Summary:")
print(f"2xx responses: {status_2xx} ({round(status_2xx/requests_total*100, 1)}%)")
print(f"3xx responses: {status_3xx} ({round(status_3xx/requests_total*100, 1)}%)")
print(f"4xx responses: {status_4xx} ({round(status_4xx/requests_total*100, 1)}%)")
print(f"5xx responses: {status_5xx} ({round(status_5xx/requests_total*100, 1)}%)")
if status_other > 0:
    print(f"Other responses: {status_other} ({round(status_other/requests_total*100, 1)}%)")

# Print detailed status code breakdown if we have multiple status codes
if len(status_codes) > 1:
    print("\nDetailed Status Codes:")
    for status, count in sorted(status_codes.items()):
        print(f"  HTTP {status}: {count} ({round(count/requests_total*100, 1)}%)")


print("Connection Statistics:")
print("Total downstream connections:", requests_total)
print("Number of re-used upstream connections:", reused_conns_count)
print('Average download time for re-used upstream connections:', reused_conns_download_times_avg, "ms")
print("Average download time for new upstream connections:", new_conns_download_times_avg, "ms")
print("Latency gain:", latency_gain, "%")
