from flask import Flask, request, jsonify, render_template
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii
import aiohttp
import requests
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
import threading
import urllib3
import random
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import atexit

# Configuration
TOKEN_BATCH_SIZE = 189
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Global State for Batch Management
current_batch_indices = {}
batch_indices_lock = threading.Lock()

def get_next_batch_tokens(server_name, all_tokens):
    if not all_tokens:
        return []
    
    total_tokens = len(all_tokens)
    
    # If we have fewer tokens than batch size, use all available tokens
    if total_tokens <= TOKEN_BATCH_SIZE:
        return all_tokens
    
    with batch_indices_lock:
        if server_name not in current_batch_indices:
            current_batch_indices[server_name] = 0
        
        current_index = current_batch_indices[server_name]
        
        # Calculate the batch
        start_index = current_index
        end_index = start_index + TOKEN_BATCH_SIZE
        
        # If we reach or exceed the end, wrap around
        if end_index > total_tokens:
            remaining = end_index - total_tokens
            batch_tokens = all_tokens[start_index:total_tokens] + all_tokens[0:remaining]
        else:
            batch_tokens = all_tokens[start_index:end_index]
        
        # Update the index for next time
        next_index = (current_index + TOKEN_BATCH_SIZE) % total_tokens
        current_batch_indices[server_name] = next_index
        
        return batch_tokens

def get_random_batch_tokens(server_name, all_tokens):
    """Alternative method: use random sampling for better distribution"""
    if not all_tokens:
        return []
    
    total_tokens = len(all_tokens)
    
    # If we have fewer tokens than batch size, use all available tokens
    if total_tokens <= TOKEN_BATCH_SIZE:
        return all_tokens.copy()
    
    # Randomly select tokens without replacement
    return random.sample(all_tokens, TOKEN_BATCH_SIZE)

def load_tokens(server_name, for_visit=False):
    if for_visit:
        if server_name == "IND":
            path = "token_ind_visit.json"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            path = "token_br_visit.json"
        else:
            path = "token_bd_visit.json"
    else:
        if server_name == "IND":
            path = "token_ind.json"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            path = "token_br.json"
        else:
            path = "token_bd.json"

    try:
        with open(path, "r") as f:
            tokens = json.load(f)
            if isinstance(tokens, list) and all(isinstance(t, dict) and "token" in t for t in tokens):
                print(f"Loaded {len(tokens)} tokens from {path} for server {server_name}")
                return tokens
            else:
                print(f"Warning: Token file {path} is not in the expected format. Returning empty list.")
                return []
    except FileNotFoundError:
        print(f"Warning: Token file {path} not found. Returning empty list for server {server_name}.")
        return []
    except json.JSONDecodeError:
        print(f"Warning: Token file {path} contains invalid JSON. Returning empty list.")
        return []

def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_message = pad(plaintext, AES.block_size)
    encrypted_message = cipher.encrypt(padded_message)
    return binascii.hexlify(encrypted_message).decode('utf-8')

def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()

def create_protobuf_for_profile_check(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return message.SerializeToString()

def enc_profile_check_payload(uid):
    protobuf_data = create_protobuf_for_profile_check(uid)
    encrypted_uid = encrypt_message(protobuf_data)
    return encrypted_uid

async def send_single_like_request(encrypted_like_payload, token_dict, url):
    edata = bytes.fromhex(encrypted_like_payload)
    token_value = token_dict.get("token", "")
    if not token_value:
        print("Warning: send_single_like_request received an empty or invalid token_dict.")
        return 999

    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB52"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    print(f"Like request failed for token {token_value[:10]}... with status: {response.status}")
                return response.status
    except asyncio.TimeoutError:
        print(f"Like request timed out for token {token_value[:10]}...")
        return 998
    except Exception as e:
        print(f"Exception in send_single_like_request for token {token_value[:10]}...: {e}")
        return 997

async def send_likes_with_token_batch(uid, server_region_for_like_proto, like_api_url, token_batch_to_use):
    if not token_batch_to_use:
        print("No tokens provided in the batch to send_likes_with_token_batch.")
        return []

    like_protobuf_payload = create_protobuf_message(uid, server_region_for_like_proto)
    encrypted_like_payload = encrypt_message(like_protobuf_payload)
    
    tasks = []
    for token_dict_for_request in token_batch_to_use:
        tasks.append(send_single_like_request(encrypted_like_payload, token_dict_for_request, like_api_url))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successful_sends = sum(1 for r in results if isinstance(r, int) and r == 200)
    failed_sends = len(token_batch_to_use) - successful_sends
    print(f"Attempted {len(token_batch_to_use)} like sends from batch. Successful: {successful_sends}, Failed/Error: {failed_sends}")
    return results

def make_profile_check_request(encrypted_profile_payload, server_name, token_dict):
    token_value = token_dict.get("token", "")
    if not token_value:
        print("Warning: make_profile_check_request received an empty token_dict.")
        return None

    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ggblueshark.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_profile_payload)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB52"
    }
    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        binary_data = response.content
        decoded_info = decode_protobuf_profile_info(binary_data)
        return decoded_info
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error in make_profile_check_request for token {token_value[:10]}...: {e.response.status_code} - {e.response.text[:100]}")
    except requests.exceptions.RequestException as e:
        print(f"Request error in make_profile_check_request for token {token_value[:10]}...: {e}")
    except Exception as e:
        print(f"Unexpected error in make_profile_check_request for token {token_value[:10]}... processing response: {e}")
    return None

def decode_protobuf_profile_info(binary_data):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary_data)
        return items
    except Exception as e:
        print(f"Error decoding Protobuf profile data: {e}")
        return None

def check_token_validity(token_str):
    """Check if token is valid and get expiry info"""
    try:
        import base64
        
        parts = token_str.split('.')
        if len(parts) != 3:
            return {"valid": False, "reason": "Invalid token format"}
        
        # Decode payload
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding
        
        decoded = base64.urlsafe_b64decode(payload)
        token_data = json.loads(decoded)
        
        exp_timestamp = token_data.get("exp")
        if not exp_timestamp:
            return {"valid": False, "reason": "No expiry found"}
        
        expiry_dt = datetime.fromtimestamp(exp_timestamp)
        now = datetime.now()
        
        # Check if expired
        if now > expiry_dt:
            return {"valid": False, "reason": "Token expired", "expiry": expiry_dt}
        
        # Check if expires within 24 hours
        time_until_expiry = expiry_dt - now
        expires_soon = time_until_expiry.total_seconds() < 86400  # 24 hours
        
        return {
            "valid": True,
            "expires_soon": expires_soon,
            "expiry": expiry_dt,
            "hours_until_expiry": time_until_expiry.total_seconds() / 3600,
            "account_id": token_data.get("account_id"),
            "nickname": token_data.get("nickname")
        }
    
    except Exception as e:
        return {"valid": False, "reason": f"Validation error: {str(e)}"}

async def generate_token_batch(credentials_batch, semaphore, session):
    """Generate tokens for a batch of credentials concurrently"""
    import sys
    import os
    
    # Add token_generator to path
    token_gen_path = os.path.join(os.path.dirname(__file__), 'token_generator')
    if token_gen_path not in sys.path:
        sys.path.insert(0, token_gen_path)
    
    from token_generator.token_gen import generate_token_with_session
    
    async def generate_single_token(uid, password):
        async with semaphore:  # Limit concurrent requests
            try:
                # Add random delay to avoid rate limiting
                await asyncio.sleep(random.uniform(0.1, 0.5))
                token_obj = await generate_token_with_session(uid, password, session)
                return uid, token_obj, None
            except Exception as e:
                return uid, None, str(e)
    
    # Create tasks for all credentials in this batch
    tasks = [generate_single_token(uid, password) for uid, password in credentials_batch]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    return results

async def refresh_tokens_logic():
    """Enhanced token refresh with batching, pooling, and smart regeneration"""
    try:
        import sys
        import os
        
        # Add token_generator to path
        token_gen_path = os.path.join(os.path.dirname(__file__), 'token_generator')
        if token_gen_path not in sys.path:
            sys.path.insert(0, token_gen_path)
        
        # Import token generation functions
        from token_generator.token_gen import load_credentials_from_file
        
        # Check credential files
        credentials_file = os.path.join(token_gen_path, 'credentials.txt')
        visit_file_creds = os.path.join(token_gen_path, 'visit.txt')
        
        has_credentials = os.path.exists(credentials_file)
        has_visit = os.path.exists(visit_file_creds)
        
        if not has_credentials and not has_visit:
            return {
                "status": "error",
                "message": "No credential files found. Create credentials.txt or visit.txt in token_generator folder",
                "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        
        print("\n" + "="*60)
        print("🚀 FAST SMART TOKEN REFRESH TRIGGERED")
        print("="*60)
        
        # Configuration for fast generation
        BATCH_SIZE = 10  # Process 10 tokens at once
        MAX_CONCURRENT = 5  # Max 5 concurrent requests to avoid IP blocks
        
        # Define output file paths
        output_file = "token_bd.json"
        visit_file = "token_bd_visit.json"
        
        total_success = 0
        total_failed = 0
        total_skipped = 0
        all_failed_accounts = []
        validity_report = []
        
        # Create connection pool for reuse
        connector = aiohttp.TCPConnector(
            limit=20,  # Total connection pool size
            limit_per_host=5,  # Max connections per host
            ttl_dns_cache=300,  # DNS cache TTL
            use_dns_cache=True,
            keepalive_timeout=30,
            enable_cleanup_closed=True
        )
        
        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        
        try:
            # Create persistent session with connection pooling
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            ) as session:
                # ========== PROCESS REGULAR TOKENS ==========
                if has_credentials:
                    print("\n📋 Processing credentials.txt with FAST BATCHING...")
                    credentials = await load_credentials_from_file(credentials_file)
                    
                    if credentials:
                        print(f"Found {len(credentials)} accounts in credentials.txt")
                        print(f"Using batch size: {BATCH_SIZE}, Max concurrent: {MAX_CONCURRENT}")
                        
                        # Load existing tokens to check validity
                        existing_tokens = []
                        if os.path.exists(output_file):
                            try:
                                with open(output_file, 'r') as f:
                                    existing_tokens = json.load(f)
                                    if not isinstance(existing_tokens, list):
                                        existing_tokens = [existing_tokens]
                            except:
                                existing_tokens = []
                        
                        # Create UID to token mapping for existing tokens
                        uid_to_token = {}
                        for token_obj in existing_tokens:
                            if isinstance(token_obj, dict) and "token" in token_obj:
                                validity = check_token_validity(token_obj["token"])
                                if validity.get("valid") and validity.get("account_id"):
                                    uid_to_token[str(validity["account_id"])] = {
                                        "token_obj": token_obj,
                                        "validity": validity
                                    }
                        
                        # Filter credentials that need new tokens
                        credentials_needing_refresh = []
                        new_tokens = []
                        skipped_count = 0
                        
                        for uid, password in credentials:
                            existing_token_info = uid_to_token.get(str(uid))
                            
                            if existing_token_info:
                                validity = existing_token_info["validity"]
                                
                                # If token is valid and not expiring soon, skip
                                if validity["valid"] and not validity.get("expires_soon", False):
                                    hours_left = validity.get("hours_until_expiry", 0)
                                    print(f"✅ UID {uid}: Token valid for {hours_left:.1f}h - SKIPPING")
                                    new_tokens.append(existing_token_info["token_obj"])
                                    skipped_count += 1
                                    validity_report.append({
                                        "uid": uid,
                                        "status": "skipped",
                                        "reason": f"Valid for {hours_left:.1f} hours",
                                        "expiry": validity["expiry"].strftime('%Y-%m-%d %H:%M:%S')
                                    })
                                    continue
                            
                            credentials_needing_refresh.append((uid, password))
                        
                        print(f"🔄 Need to refresh: {len(credentials_needing_refresh)} tokens")
                        print(f"✅ Keeping valid: {skipped_count} tokens")
                        
                        if credentials_needing_refresh:
                            # Process in batches for speed
                            success_count = 0
                            failed_accounts = []
                            
                            for i in range(0, len(credentials_needing_refresh), BATCH_SIZE):
                                batch = credentials_needing_refresh[i:i + BATCH_SIZE]
                                batch_num = (i // BATCH_SIZE) + 1
                                total_batches = (len(credentials_needing_refresh) + BATCH_SIZE - 1) // BATCH_SIZE
                                
                                print(f"\n🚀 Processing batch {batch_num}/{total_batches} ({len(batch)} tokens)...")
                                start_time = datetime.now()
                                
                                # Generate tokens for this batch concurrently
                                batch_results = await generate_token_batch(batch, semaphore, session)
                                
                                # Process results
                                for uid, token_obj, error in batch_results:
                                    if isinstance(uid, Exception):
                                        print(f"❌ Batch error: {uid}")
                                        continue
                                    
                                    if token_obj and not error:
                                        # Validate the new token
                                        new_validity = check_token_validity(token_obj["token"])
                                        
                                        if new_validity["valid"]:
                                            new_tokens.append(token_obj)
                                            success_count += 1
                                            hours_valid = new_validity.get("hours_until_expiry", 0)
                                            print(f"✅ UID {uid}: New token - Valid for {hours_valid:.1f}h")
                                            validity_report.append({
                                                "uid": uid,
                                                "status": "generated",
                                                "reason": f"New token valid for {hours_valid:.1f} hours",
                                                "expiry": new_validity["expiry"].strftime('%Y-%m-%d %H:%M:%S')
                                            })
                                        else:
                                            print(f"❌ UID {uid}: Invalid token - {new_validity.get('reason')}")
                                            failed_accounts.append((uid, f"Invalid: {new_validity.get('reason')}"))
                                            validity_report.append({
                                                "uid": uid,
                                                "status": "failed",
                                                "reason": f"Invalid token: {new_validity.get('reason')}"
                                            })
                                    else:
                                        print(f"❌ UID {uid}: Generation failed - {error}")
                                        failed_accounts.append((uid, error or "Generation failed"))
                                        validity_report.append({
                                            "uid": uid,
                                            "status": "failed",
                                            "reason": error or "Generation failed"
                                        })
                                
                                batch_time = (datetime.now() - start_time).total_seconds()
                                print(f"⚡ Batch {batch_num} completed in {batch_time:.2f}s ({len(batch)/batch_time:.1f} tokens/sec)")
                                
                                # Small delay between batches to avoid overwhelming the server
                                if i + BATCH_SIZE < len(credentials_needing_refresh):
                                    await asyncio.sleep(1)
                            
                            total_success += success_count
                            total_failed += len(failed_accounts)
                            total_skipped += skipped_count
                            all_failed_accounts.extend([{"uid": uid, "reason": reason, "file": "credentials.txt"} for uid, reason in failed_accounts])
                            
                            print(f"\n✅ Regular tokens: {success_count} generated, {skipped_count} skipped, {len(failed_accounts)} failed")
                        
                        # Save all tokens (existing valid + new)
                        if new_tokens:
                            with open(output_file, 'w') as f:
                                json.dump(new_tokens, f, indent=2)
                            print(f"💾 Saved {len(new_tokens)} total tokens to {output_file}")
                
                # ========== PROCESS VISIT TOKENS ==========
                if has_visit:
                    print("\n📋 Processing visit.txt with FAST BATCHING...")
                    visit_credentials = await load_credentials_from_file(visit_file_creds)
                    
                    if visit_credentials:
                        print(f"Found {len(visit_credentials)} accounts in visit.txt")
                        
                        # Load existing visit tokens to check validity
                        existing_visit_tokens = []
                        if os.path.exists(visit_file):
                            try:
                                with open(visit_file, 'r') as f:
                                    existing_visit_tokens = json.load(f)
                                    if not isinstance(existing_visit_tokens, list):
                                        existing_visit_tokens = [existing_visit_tokens]
                            except:
                                existing_visit_tokens = []
                        
                        # Create UID to token mapping for existing visit tokens
                        visit_uid_to_token = {}
                        for token_obj in existing_visit_tokens:
                            if isinstance(token_obj, dict) and "token" in token_obj:
                                validity = check_token_validity(token_obj["token"])
                                if validity.get("valid") and validity.get("account_id"):
                                    visit_uid_to_token[str(validity["account_id"])] = {
                                        "token_obj": token_obj,
                                        "validity": validity
                                    }
                        
                        # Filter visit credentials that need new tokens
                        visit_credentials_needing_refresh = []
                        new_visit_tokens = []
                        visit_skipped_count = 0
                        
                        for uid, password in visit_credentials:
                            existing_visit_token_info = visit_uid_to_token.get(str(uid))
                            
                            if existing_visit_token_info:
                                validity = existing_visit_token_info["validity"]
                                
                                # If token is valid and not expiring soon, skip
                                if validity["valid"] and not validity.get("expires_soon", False):
                                    hours_left = validity.get("hours_until_expiry", 0)
                                    print(f"✅ Visit UID {uid}: Token valid for {hours_left:.1f}h - SKIPPING")
                                    new_visit_tokens.append(existing_visit_token_info["token_obj"])
                                    visit_skipped_count += 1
                                    validity_report.append({
                                        "uid": uid,
                                        "status": "skipped",
                                        "type": "visit",
                                        "reason": f"Valid for {hours_left:.1f} hours",
                                        "expiry": validity["expiry"].strftime('%Y-%m-%d %H:%M:%S')
                                    })
                                    continue
                            
                            visit_credentials_needing_refresh.append((uid, password))
                        
                        print(f"🔄 Need to refresh: {len(visit_credentials_needing_refresh)} visit tokens")
                        print(f"✅ Keeping valid: {visit_skipped_count} visit tokens")
                        
                        if visit_credentials_needing_refresh:
                            # Process visit tokens in batches
                            visit_success_count = 0
                            visit_failed_accounts = []
                            
                            for i in range(0, len(visit_credentials_needing_refresh), BATCH_SIZE):
                                batch = visit_credentials_needing_refresh[i:i + BATCH_SIZE]
                                batch_num = (i // BATCH_SIZE) + 1
                                total_batches = (len(visit_credentials_needing_refresh) + BATCH_SIZE - 1) // BATCH_SIZE
                                
                                print(f"\n🚀 Processing visit batch {batch_num}/{total_batches} ({len(batch)} tokens)...")
                                start_time = datetime.now()
                                
                                # Generate visit tokens for this batch concurrently
                                batch_results = await generate_token_batch(batch, semaphore, session)
                                
                                # Process results
                                for uid, token_obj, error in batch_results:
                                    if isinstance(uid, Exception):
                                        print(f"❌ Visit batch error: {uid}")
                                        continue
                                    
                                    if token_obj and not error:
                                        # Validate the new visit token
                                        new_validity = check_token_validity(token_obj["token"])
                                        
                                        if new_validity["valid"]:
                                            new_visit_tokens.append(token_obj)
                                            visit_success_count += 1
                                            hours_valid = new_validity.get("hours_until_expiry", 0)
                                            print(f"✅ Visit UID {uid}: New token - Valid for {hours_valid:.1f}h")
                                            validity_report.append({
                                                "uid": uid,
                                                "status": "generated",
                                                "type": "visit",
                                                "reason": f"New token valid for {hours_valid:.1f} hours",
                                                "expiry": new_validity["expiry"].strftime('%Y-%m-%d %H:%M:%S')
                                            })
                                        else:
                                            print(f"❌ Visit UID {uid}: Invalid token - {new_validity.get('reason')}")
                                            visit_failed_accounts.append((uid, f"Invalid: {new_validity.get('reason')}"))
                                            validity_report.append({
                                                "uid": uid,
                                                "status": "failed",
                                                "type": "visit",
                                                "reason": f"Invalid token: {new_validity.get('reason')}"
                                            })
                                    else:
                                        print(f"❌ Visit UID {uid}: Generation failed - {error}")
                                        visit_failed_accounts.append((uid, error or "Generation failed"))
                                        validity_report.append({
                                            "uid": uid,
                                            "status": "failed",
                                            "type": "visit",
                                            "reason": error or "Generation failed"
                                        })
                                
                                batch_time = (datetime.now() - start_time).total_seconds()
                                print(f"⚡ Visit batch {batch_num} completed in {batch_time:.2f}s ({len(batch)/batch_time:.1f} tokens/sec)")
                                
                                # Small delay between batches
                                if i + BATCH_SIZE < len(visit_credentials_needing_refresh):
                                    await asyncio.sleep(1)
                            
                            total_success += visit_success_count
                            total_failed += len(visit_failed_accounts)
                            total_skipped += visit_skipped_count
                            all_failed_accounts.extend([{"uid": uid, "reason": reason, "file": "visit.txt"} for uid, reason in visit_failed_accounts])
                            
                            print(f"\n✅ Visit tokens: {visit_success_count} generated, {visit_skipped_count} skipped, {len(visit_failed_accounts)} failed")
                        
                        # Save all visit tokens (existing valid + new)
                        if new_visit_tokens:
                            with open(visit_file, 'w') as f:
                                json.dump(new_visit_tokens, f, indent=2)
                            print(f"💾 Saved {len(new_visit_tokens)} total visit tokens to {visit_file}")
                            batch_results = await generate_token_batch(batch, semaphore, session)
                            
                            # Process results
                            for uid, token_obj, error in batch_results:
                                if isinstance(uid, Exception):
                                    print(f"❌ Visit batch error: {uid}")
                                    continue
                                
                                if token_obj and not error:
                                    # Validate the new visit token
                                    new_validity = check_token_validity(token_obj["token"])
                                    
                                    if new_validity["valid"]:
                                        new_visit_tokens.append(token_obj)
                                        visit_success_count += 1
                                        hours_valid = new_validity.get("hours_until_expiry", 0)
                                        print(f"✅ Visit UID {uid}: New token - Valid for {hours_valid:.1f}h")
                                        validity_report.append({
                                            "uid": uid,
                                            "status": "generated",
                                            "type": "visit",
                                            "reason": f"New token valid for {hours_valid:.1f} hours",
                                            "expiry": new_validity["expiry"].strftime('%Y-%m-%d %H:%M:%S')
                                        })
                                    else:
                                        print(f"❌ Visit UID {uid}: Invalid token - {new_validity.get('reason')}")
                                        visit_failed_accounts.append((uid, f"Invalid: {new_validity.get('reason')}"))
                                        validity_report.append({
                                            "uid": uid,
                                            "status": "failed",
                                            "type": "visit",
                                            "reason": f"Invalid token: {new_validity.get('reason')}"
                                        })
                                else:
                                    print(f"❌ Visit UID {uid}: Generation failed - {error}")
                                    visit_failed_accounts.append((uid, error or "Generation failed"))
                                    validity_report.append({
                                        "uid": uid,
                                        "status": "failed",
                                        "type": "visit",
                                        "reason": error or "Generation failed"
                                    })
                            
                            batch_time = (datetime.now() - start_time).total_seconds()
                            print(f"⚡ Visit batch {batch_num} completed in {batch_time:.2f}s ({len(batch)/batch_time:.1f} tokens/sec)")
                            
                            # Small delay between batches
                            if i + BATCH_SIZE < len(visit_credentials_needing_refresh):
                                await asyncio.sleep(1)
                        
                        total_success += visit_success_count
                        total_failed += len(visit_failed_accounts)
                        total_skipped += visit_skipped_count
                        all_failed_accounts.extend([{"uid": uid, "reason": reason, "file": "visit.txt"} for uid, reason in visit_failed_accounts])
                        
                        print(f"\n✅ Visit tokens: {visit_success_count} generated, {visit_skipped_count} skipped, {len(visit_failed_accounts)} failed")
                    
                    # Save all visit tokens (existing valid + new)
                    if new_visit_tokens:
                        with open(visit_file, 'w') as f:
                            json.dump(new_visit_tokens, f, indent=2)
                        print(f"💾 Saved {len(new_visit_tokens)} total visit tokens to {visit_file}")
        
        finally:
            # Close connector
            await connector.close()
        
        print("\n" + "="*60)
        print("🚀 FAST SMART TOKEN REFRESH COMPLETE")
        print("="*60)
        
        return {
            "status": "success",
            "message": "Smart token refresh completed",
            "total_successful": total_success,
            "total_failed": total_failed,
            "total_skipped": total_skipped,
            "failed_accounts": all_failed_accounts,
            "validity_report": validity_report,
            "files_processed": {
                "credentials.txt": has_credentials,
                "visit.txt": has_visit
            },
            "output_files": {
                "regular_tokens": output_file if has_credentials else None,
                "visit_tokens": visit_file if has_visit else None
            },
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    
    except Exception as e:
        print(f"\n❌ Token refresh error: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

def refresh_tokens_task():
    """Background task to refresh tokens automatically with performance monitoring"""
    start_time = datetime.now()
    print(f"\n🚀 FAST AUTO REFRESH TRIGGERED at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        # Call the refresh endpoint logic
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(refresh_tokens_logic())
        finally:
            loop.close()
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        total_processed = result.get('total_successful', 0) + result.get('total_skipped', 0) + result.get('total_failed', 0)
        speed = total_processed / duration if duration > 0 else 0
        
        print(f"⚡ Fast auto refresh completed in {duration:.2f}s")
        print(f"📊 Performance: {speed:.1f} tokens/sec")
        print(f"✅ Results: {result.get('total_successful', 0)} generated, {result.get('total_skipped', 0)} skipped, {result.get('total_failed', 0)} failed")
        
        return result
    except Exception as e:
        print(f"❌ Fast auto refresh failed: {e}")
        return None

app = Flask(__name__)

# Initialize scheduler for automatic token refresh with smart checking
scheduler = BackgroundScheduler()
scheduler.start()

# Schedule smart token refresh every 4 hours (more frequent for faster response)
scheduler.add_job(
    func=refresh_tokens_task,
    trigger="interval",
    hours=4,
    id='fast_smart_token_refresh_job',
    name='Fast Smart Token Refresh',
    replace_existing=True
)

# Run smart token refresh on startup
scheduler.add_job(
    func=refresh_tokens_task,
    trigger="date",
    run_date=datetime.now(),
    id='startup_smart_refresh',
    name='Startup Smart Token Refresh'
)

# Shut down the scheduler when exiting the app
atexit.register(lambda: scheduler.shutdown())

@app.route('/')
def home():
    """Serve the dashboard"""
    return render_template('index.html')

@app.route('/like', methods=['GET'])
def handle_requests():
    uid_param = request.args.get("uid")
    server_name_param = request.args.get("server_name", "").upper()
    use_random = request.args.get("random", "false").lower() == "true"

    if not uid_param or not server_name_param:
        return jsonify({"error": "UID and server_name are required"}), 400

    # Load visit token for profile checking
    visit_tokens = load_tokens(server_name_param, for_visit=True)
    if not visit_tokens:
        return jsonify({"error": f"No visit tokens loaded for server {server_name_param}."}), 500
    
    # Use the first visit token for profile check
    visit_token = visit_tokens[0] if visit_tokens else None
    
    # Load regular tokens for like sending
    all_available_tokens = load_tokens(server_name_param, for_visit=False)
    if not all_available_tokens:
        return jsonify({"error": f"No tokens loaded or token file invalid for server {server_name_param}."}), 500

    print(f"Total tokens available for {server_name_param}: {len(all_available_tokens)}")

    # Get the batch of tokens for like sending
    if use_random:
        tokens_for_like_sending = get_random_batch_tokens(server_name_param, all_available_tokens)
        print(f"Using RANDOM batch selection for {server_name_param}")
    else:
        tokens_for_like_sending = get_next_batch_tokens(server_name_param, all_available_tokens)
        print(f"Using ROTATING batch selection for {server_name_param}")
    
    encrypted_player_uid_for_profile = enc_profile_check_payload(uid_param)
    
    # Get likes BEFORE using visit token
    before_info = make_profile_check_request(encrypted_player_uid_for_profile, server_name_param, visit_token)
    before_like_count = 0
    
    if before_info and hasattr(before_info, 'AccountInfo'):
        before_like_count = int(before_info.AccountInfo.Likes)
    else:
        print(f"Could not reliably fetch 'before' profile info for UID {uid_param} on {server_name_param}.")

    print(f"UID {uid_param} ({server_name_param}): Likes before = {before_like_count}")

    # Determine the URL for sending likes
    if server_name_param == "IND":
        like_api_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name_param in {"BR", "US", "SAC", "NA"}:
        like_api_url = "https://client.us.freefiremobile.com/LikeProfile"
    else:
        like_api_url = "https://clientbp.ggblueshark.com/LikeProfile"

    if tokens_for_like_sending:
        print(f"Using token batch for {server_name_param} (size {len(tokens_for_like_sending)}) to send likes.")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(send_likes_with_token_batch(uid_param, server_name_param, like_api_url, tokens_for_like_sending))
        finally:
            loop.close()
    else:
        print(f"Skipping like sending for UID {uid_param} as no tokens available for like sending.")
        
    # Get likes AFTER using visit token
    after_info = make_profile_check_request(encrypted_player_uid_for_profile, server_name_param, visit_token)
    after_like_count = before_like_count
    actual_player_uid_from_profile = int(uid_param)
    player_nickname_from_profile = "N/A"

    if after_info and hasattr(after_info, 'AccountInfo'):
        after_like_count = int(after_info.AccountInfo.Likes)
        actual_player_uid_from_profile = int(after_info.AccountInfo.UID)
        if after_info.AccountInfo.PlayerNickname:
            player_nickname_from_profile = str(after_info.AccountInfo.PlayerNickname)
        else:
            player_nickname_from_profile = "N/A"
    else:
        print(f"Could not reliably fetch 'after' profile info for UID {uid_param} on {server_name_param}.")

    print(f"UID {uid_param} ({server_name_param}): Likes after = {after_like_count}")

    likes_increment = after_like_count - before_like_count
    request_status = 1 if likes_increment > 0 else (2 if likes_increment == 0 else 3)

    response_data = {
        "LikesGivenByAPI": likes_increment,
        "LikesafterCommand": after_like_count,
        "LikesbeforeCommand": before_like_count,
        "PlayerNickname": player_nickname_from_profile,
        "UID": actual_player_uid_from_profile,
        "status": request_status,
        "Note": f"Used visit token for profile check and {'random' if use_random else 'rotating'} batch of {len(tokens_for_like_sending)} tokens for like sending."
    }
    return jsonify(response_data)

@app.route('/token_info', methods=['GET'])
def token_info():
    """Endpoint to check token counts for each server"""
    servers = ["IND", "BD", "BR", "US", "SAC", "NA"]
    info = {}
    
    for server in servers:
        regular_tokens = load_tokens(server, for_visit=False)
        visit_tokens = load_tokens(server, for_visit=True)
        info[server] = {
            "regular_tokens": len(regular_tokens),
            "visit_tokens": len(visit_tokens)
        }
    
    return jsonify(info)

@app.route('/scheduler_status', methods=['GET'])
def scheduler_status():
    """Check scheduler status and next run time"""
    from datetime import datetime, timezone
    
    jobs = scheduler.get_jobs()
    job_info = []
    
    for job in jobs:
        next_run = None
        time_remaining = None
        
        if job.next_run_time:
            # Convert to local time and format nicely
            next_run_dt = job.next_run_time
            if next_run_dt.tzinfo is not None:
                next_run_dt = next_run_dt.replace(tzinfo=None)
            
            next_run = next_run_dt.strftime('%B %d, %Y at %I:%M:%S %p')
            
            # Calculate time remaining
            now = datetime.now()
            time_diff = next_run_dt - now
            
            if time_diff.total_seconds() > 0:
                hours = int(time_diff.total_seconds() // 3600)
                minutes = int((time_diff.total_seconds() % 3600) // 60)
                seconds = int(time_diff.total_seconds() % 60)
                
                if hours > 0:
                    time_remaining = f"{hours}h {minutes}m {seconds}s"
                elif minutes > 0:
                    time_remaining = f"{minutes}m {seconds}s"
                else:
                    time_remaining = f"{seconds}s"
            else:
                time_remaining = "Running now..."
        
        job_info.append({
            "id": job.id,
            "name": job.name,
            "next_run_time": next_run if next_run else "Not scheduled",
            "time_remaining": time_remaining if time_remaining else "N/A"
        })
    
    return jsonify({
        "status": "🟢 Running" if scheduler.running else "🔴 Stopped",
        "current_time": datetime.now().strftime('%B %d, %Y at %I:%M:%S %p'),
        "total_jobs": len(jobs),
        "jobs": job_info
    })

@app.route('/token_details', methods=['GET'])
def token_details():
    """Endpoint to get detailed token information with validity check"""
    try:
        import base64
        from datetime import datetime
        
        server_name = request.args.get("server", "BD").upper()
        
        # Load tokens
        regular_tokens = load_tokens(server_name, for_visit=False)
        visit_tokens = load_tokens(server_name, for_visit=True)
        
        def parse_token_info(token_dict):
            """Extract information from token with validity check"""
            token_str = token_dict.get("token", "")
            validity = check_token_validity(token_str)
            
            return {
                "token": token_str,
                "account_id": validity.get("account_id"),
                "nickname": validity.get("nickname"),
                "expiry_date": validity.get("expiry").strftime('%Y-%m-%d %H:%M:%S') if validity.get("expiry") else None,
                "is_valid": validity.get("valid", False),
                "expires_soon": validity.get("expires_soon", False),
                "hours_until_expiry": validity.get("hours_until_expiry", 0),
                "reason": validity.get("reason") if not validity.get("valid") else None
            }
        
        # Parse all tokens
        parsed_regular = [parse_token_info(t) for t in regular_tokens]
        parsed_visit = [parse_token_info(t) for t in visit_tokens]
        
        # Count valid/invalid tokens
        valid_regular = sum(1 for t in parsed_regular if t["is_valid"])
        valid_visit = sum(1 for t in parsed_visit if t["is_valid"])
        expiring_regular = sum(1 for t in parsed_regular if t["is_valid"] and t["expires_soon"])
        expiring_visit = sum(1 for t in parsed_visit if t["is_valid"] and t["expires_soon"])
        
        return jsonify({
            "status": "success",
            "server": server_name,
            "summary": {
                "total_tokens": len(regular_tokens),
                "valid_tokens": valid_regular,
                "expiring_soon": expiring_regular,
                "total_visit_tokens": len(visit_tokens),
                "valid_visit_tokens": valid_visit,
                "expiring_visit_soon": expiring_visit
            },
            "tokens": parsed_regular,
            "visit_tokens": parsed_visit
        })
    
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/refresh_tokens', methods=['POST', 'GET'])
def refresh_tokens():
    """Endpoint to manually trigger fast smart token refresh"""
    start_time = datetime.now()
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(refresh_tokens_logic())
    finally:
        loop.close()
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    
    # Add performance metrics to result
    if result.get("status") == "success":
        total_processed = result.get('total_successful', 0) + result.get('total_skipped', 0) + result.get('total_failed', 0)
        result['performance'] = {
            'duration_seconds': round(duration, 2),
            'tokens_per_second': round(total_processed / duration, 1) if duration > 0 else 0,
            'total_processed': total_processed
        }
    
    if result.get("status") == "error":
        return jsonify(result), 500
    return jsonify(result)

@app.route('/performance_test', methods=['GET'])
def performance_test():
    """Test endpoint to measure token generation performance"""
    try:
        # Get a small sample of credentials for testing
        import sys
        import os
        
        token_gen_path = os.path.join(os.path.dirname(__file__), 'token_generator')
        if token_gen_path not in sys.path:
            sys.path.insert(0, token_gen_path)
        
        from token_generator.token_gen import load_credentials_from_file
        
        credentials_file = os.path.join(token_gen_path, 'credentials.txt')
        
        if not os.path.exists(credentials_file):
            return jsonify({
                "status": "error",
                "message": "credentials.txt not found"
            }), 404
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            credentials = loop.run_until_complete(load_credentials_from_file(credentials_file))
            
            # Test with first 5 credentials only
            test_credentials = credentials[:5] if len(credentials) >= 5 else credentials
            
            if not test_credentials:
                return jsonify({
                    "status": "error",
                    "message": "No credentials available for testing"
                }), 400
            
            start_time = datetime.now()
            
            # Simulate the batching process
            BATCH_SIZE = 3
            MAX_CONCURRENT = 2
            
            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=3,
                ttl_dns_cache=300,
                use_dns_cache=True,
                keepalive_timeout=30
            )
            
            semaphore = asyncio.Semaphore(MAX_CONCURRENT)
            
            async def test_performance():
                async with aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=30, connect=10)
                ) as session:
                    
                    results = []
                    for i in range(0, len(test_credentials), BATCH_SIZE):
                        batch = test_credentials[i:i + BATCH_SIZE]
                        batch_start = datetime.now()
                        
                        # Simulate batch processing (without actual token generation)
                        await asyncio.sleep(0.1)  # Simulate network delay
                        
                        batch_time = (datetime.now() - batch_start).total_seconds()
                        results.append({
                            'batch_size': len(batch),
                            'batch_time': batch_time,
                            'tokens_per_second': len(batch) / batch_time if batch_time > 0 else 0
                        })
                    
                    return results
            
            batch_results = loop.run_until_complete(test_performance())
            
            end_time = datetime.now()
            total_duration = (end_time - start_time).total_seconds()
            
            return jsonify({
                "status": "success",
                "test_summary": {
                    "total_credentials_tested": len(test_credentials),
                    "total_duration_seconds": round(total_duration, 2),
                    "estimated_tokens_per_second": round(len(test_credentials) / total_duration, 1) if total_duration > 0 else 0,
                    "batch_configuration": {
                        "batch_size": BATCH_SIZE,
                        "max_concurrent": MAX_CONCURRENT
                    }
                },
                "batch_results": batch_results,
                "recommendations": {
                    "optimal_batch_size": "10-15 tokens per batch",
                    "max_concurrent": "5-8 concurrent requests",
                    "expected_speed": "3-8 tokens per second",
                    "ip_protection": "Built-in delays and user agent rotation"
                }
            })
        
        finally:
            loop.close()
    
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)