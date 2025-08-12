import json
import audioop
import re

from config import (
    MASTER_SYSTEM_PROMPT,
    LANGUAGE_SYSTEM_PROMPT,
    logger
)

def decode_pcmu_to_pcm16(ulaw_bytes: bytes) -> bytes:
    return audioop.ulaw2lin(ulaw_bytes, 2)

def encode_pcm16_to_pcmu(pcm16_bytes: bytes) -> bytes:
    return audioop.lin2ulaw(pcm16_bytes, 2)

def format_json(obj: dict) -> str:
    return json.dumps(obj, indent=2)

def create_final_system_prompt(admin_prompt, language=None, customer_data=None, agent_name=None, company_name=None):
    base_prompt = LANGUAGE_SYSTEM_PROMPT.format(language=language) if language else MASTER_SYSTEM_PROMPT

    if agent_name:
        admin_prompt = admin_prompt.replace("[AGENT_NAME]", agent_name)
    if company_name:
        admin_prompt = admin_prompt.replace("[COMPANY_NAME]", company_name)
        admin_prompt = admin_prompt.replace("Our Company", company_name)

    customer_instructions = ""
    if customer_data:
        try:
            data_pairs = [pair.strip() for pair in customer_data.split(';')]
            data_dict = {}
            for pair in data_pairs:
                if ':' in pair:
                    key, value = pair.split(':', 1)
                    data_dict[key.strip()] = value.strip()

            if data_dict:
                customer_instructions = "\n\n[CUSTOMER DATA - USE WHEN APPROPRIATE]\n"
                for key, value in data_dict.items():
                    customer_instructions += f"{key}: {value}\n"
                customer_instructions += "Use this customer data to personalize the conversation when relevant."
        except Exception as e:
            logger.warning(f"Error parsing customer data: {e}")

    return f"""[TIER 1 - MASTER INSTRUCTIONS - HIGHEST PRIORITY]
{base_prompt}

[TIER 2 - ADMIN INSTRUCTIONS]
{admin_prompt}{customer_instructions}

[HIERARCHY ENFORCEMENT]
In case of any conflict between Tier 1 and Tier 2 instructions, Tier 1 (Master) instructions 
MUST ALWAYS take precedence and override any conflicting Tier 2 instructions."""

def parse_iso8601_duration(duration_str: str) -> float:
    match = re.match(r'P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', duration_str)
    if not match:
        raise ValueError(f"Invalid ISO 8601 duration format: {duration_str}")
    days, hours, minutes, seconds = match.groups()
    total_seconds = 0
    if days:
        total_seconds += int(days) * 86400
    if hours:
        total_seconds += int(hours) * 3600
    if minutes:
        total_seconds += int(minutes) * 60
    if seconds:
        total_seconds += float(seconds)
    return total_seconds
