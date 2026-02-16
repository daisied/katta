"""
Hourly Tech/AI/News Tracker
Checks twitter for major news every hour
Only alerts if something significant is found
"""

import subprocess
from datetime import datetime

SEARCH_TERMS = [
    # gaming
    "GTA 6 OR GTA VI",
    "Switch 2 OR Nintendo Switch 2",
    "PS5 Pro OR Xbox Next",

    # ai / tech
    "OpenAI OR GPT-5 OR GPT-4.5",
    "Nvidia",
    "Apple OR Samsung OR Google Pixel",
    "iPhone Fold OR iPhone Flip",
    "tech OR ai",  # general catch-all

    # major news (big stuff only)
    "election OR president",
    "stock market crash OR recession",
    "war OR ceasefire OR peace deal",
    "pandemic OR virus outbreak",
    "SpaceX OR NASA OR moon landing"
]

def bird_search(query):
    """Run bird search and return results"""
    try:
        result = subprocess.run(
            ["bird", "search", query],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout
    except Exception as e:
        return f"Error: {e}"

def is_significant(text):
    """Filter for significant news only"""
    significant_keywords = [
        "leak", "reveal", "announce", "confirm", "release date",
        "specs", "price", "official", "rumor", "coming soon",
        "prototype", "exclusive", "breakthrough", "new model",
        "2025", "2026", "2027", "crash", "outbreak", "ceasefire",
        "election result", "win", "lose", "launch"
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in significant_keywords)

def is_noise(text):
    """Filter out noise - milestone posts, drama, recycled news"""
    noise_keywords = [
        "milestone", "users", "revenue", "earnings", "quarter",
        "interview", "opinion", "think", "believe",
        "drama", "controversy", "scandal", " response to"
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in noise_keywords)

def main():
    print(f"\n[{datetime.now().strftime('%H:%M')}] Checking news...")
    
    significant_updates = []
    
    for term in SEARCH_TERMS:
        output = bird_search(term)
        if output and is_significant(output) and not is_noise(output):
            significant_updates.append(f"[{term}]: {output[:300]}")
    
    if significant_updates:
        print("\n🚨 SIGNIFICANT UPDATES:")
        for update in significant_updates[:5]:
            print(update)
        print("\n---")
    else:
        print("No significant updates found. Staying silent.")
    
    return significant_updates

if __name__ == "__main__":
    main()