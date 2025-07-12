from datetime import datetime
from dotenv import load_dotenv
from notion_client import Client
import os
import time
import ast
import re

import json
from google import genai
import asyncio
import jsonify

from datetime import datetime

load_dotenv()

# Notion
notion = Client(auth=os.getenv("NOTION_API_KEY"))
TASKS_DB_ID = os.getenv("TASKS_DB_ID")
WRITE_DAY_DB = os.getenv("WRITE_DAY_DB")

# Gemini
gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Features:
    # Allow for multiple days
    # See collisions and tell the user about them in a cool way


SYSTEM_PROMPT = """ You are Gemini Flash, an expert in personal time management and calendar planning.  You will receive a free-form description of your client's “ideal day,” and must translate that into a conflict-free, realistic schedule.  Follow these guidelines exactly:

1. **Parse Fixed-Time Events**  
   - Detect any explicit time references (e.g. “band practice from 1-3 PM,” “flight at 08:30”) and treat these as immovable.  
   - Also recognize implied fixed events (“lunch at noon,” “my class starts at 9”) and block them accordingly.

2. **Identify and Durate Flexible Tasks**  
   - For tasks without a specified duration, default to **1 hour**.  
   - If the user says “for 30 minutes” or “two hours,” respect that exactly.  
   - If they ask for “a bit of reading,” assume a **minimum** of 30 minutes but confirm if needed.

3. **Prioritization & Ordering**  
   - Look for priority cues (“I **need** to finish my homework,” “I'd **like** to go for a run”) and sort tasks so that higher-priority items come earlier in the day or before lower-priority ones.  
   - If there's more work than can realistically fit, schedule as many high-priority items as possible, then stop—do not overfill the day.

4. **No Overlaps; Enforce Buffers**  
   - Never schedule two things at once.  
   - If two events are close together, like 8-9AM and then 9-10AM, make the first event from 8-8:50AM and keep the second one as is to allow for down time. DO NOT BLOCK THIS TIME, JUST LEAVE A GAP
   - If you have something like '["School", 2025, 7, 8, 13, 0, 14, 50]' and '["School", 2025, 7, 8, 15, 0, 15, 0]' in the same response (you can see how they can overlap), just make it ["School", 2025, 7, 8, 13, 0, 15, 0]
   - Insert one longer break of **45 60 minutes** for meals around midday (adjust earlier or later based on existing events).

5. **Respect User Preferences & Constraints**  
   - If the user indicates they are more productive in the morning or that they want “exercise before breakfast,” honor those.  
   - Honor any “no-meetings” windows (e.g. “I don't want anything before 9 AM” or “I'm off after 6 PM”).

6. **Time Zone & Date Context**  
   - Assume the user's locale/time-zone unless otherwise specified.  
   - Convert all times into a **24-hour clock**:  


7. **Output Format**  
   - Return **only** a JSON-style 2D array of events:  
     ```
     [
       ['name_of_event', Year, month, day, startHour, startMinute, endHour, endMinute],
       ['another_event', Year, month, day, startHour, startMinute, endHour, endMinute],
       …  
     ]
     ```  
   - Do **not** add any explanatory text, questions, or clarifications.

8. **Error Handling & Edge Cases**  
   - If two fixed-time events conflict, choose the one with the clearer timestamp and discard or flag the other.  
   - If you cannot fit a high-priority task at all, schedule it at the very end of the day and flag it as “(unscheduled).”  
   - For “all-day” events (e.g. “conference,” “holiday”), create a single block from 00:00 to 23:59.

9. Write response as below, NOT AS JSON DO NOT INCLUDE JSON ONLY ```
   -  ```[["task", startHr, startMin, endHr, endMin]]``` 

10. ** Days Months and Years**
    - You will be provided with the current date and time, if the user does not specify the day, then assume they are talking about the current day (for example if it's July 8th 2025, it'll be 2025, 7, 8).
    - The date is given in the format "year-month-day"
    - The user may specify when they want to do certain things, for example "On the 25th August, I have this event", if they do not specify further, make it a full day event, for example "I have a marathon event on August 25th" should give ["Marathon", 2025, 8, 25, 0, 0, 23, 59]
    - If they do specify, for example "I have a marathon event on August 25th from 4-8pm", then you should give ["Marathon", 2025, 8, 25, 16, 0, 20, 0]
    - The user can also specify a specific number of days, for example "in three days I will have this event". If they do not specify what time, then you should assume a full day event. If they do, set the time as appropriate
    - The user can also specify words like "tomorrow" or "in a fortnight", which mean 1 day and 14 days respectively. Account for these accordingly
    - The user can also specify things like "next week", if they do not provide a day, assume they mean the day 7 days from the current day, so 25th July goes to 1st August, and 31st December 2025 goes to 7th January
    - If they do provide a day, like "next week Friday", you can use the current date and time, for example 12th July 2025 is a Saturday, and realise that "next friday" refers to 18th July 2025


"""

# Prompt gemini with my day

def create_task(title: str, startTime: datetime, endTime: datetime):
    return notion.pages.create(
        parent={"database_id": TASKS_DB_ID},
        properties={
            "Name": {
                "title": [{"text": {"content": title}}]
            },
            "Date": {
                "date": {"start": startTime.isoformat(), "end": endTime.isoformat()}
            }
        }
    )


def fetch_pending():
    response = notion.databases.query(
        database_id=WRITE_DAY_DB,
        filter={
            "and": [
                { "property": "Send to Calendar", "checkbox": {"equals": True} },
                { "property": "Processed", "checkbox": {"equals": False} }
            ]
        }
    )
    return response["results"]

def clean_gemini_output(raw: str) -> list:

    # 1) Remove any Markdown fences
    text = re.sub(r'```(?:json)?', '', raw)

    # 2) Extract the first bracketed array
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        raise ValueError("No JSON-like array found in Gemini output.")
    arr_text = m.group(0)

    # 3) Remove leading zeros in numbers (06 → 6, 09 → 9, etc.)
    #    Matches numbers after [ or , and before , or ].
    arr_text = re.sub(
        r'(?<=\[|,)\s*0+(\d+)',
        r' \1',
        arr_text
    )

    # 4) Safely parse it
    try:
        return ast.literal_eval(arr_text)
    except Exception as e:
        raise ValueError(f"Failed to parse cleaned array: {e}")

def gemini_chat(userInput: str):

    fullPrompt = f"{SYSTEM_PROMPT}\nThe current date is {str(datetime.now())[:10]}\nUser: {userInput}"
    response = gemini.models.generate_content(
        model="gemini-2.0-flash",
        contents=fullPrompt
    )
    raw = response.text
    print("Raw Gemini response:", raw)

    # Clean & parse
    try:
        schedule = clean_gemini_output(raw)
    except ValueError as err:
        print("Error cleaning Gemini output:", err)
        return []
    return schedule

def create_events(eventArray):

    for event in eventArray:

        title, year, month, day, startH, startM, endH, endM, = event

        # Full day event
        if startH == 0 and startM == 0 and endH == 23 and endM == 59:
            notion.pages.create(
                parent={"database_id": TASKS_DB_ID},
                properties={
                    "Name": {"title": [{"text": {"content": title}}]},
                    "Date": {"date": {"start": f"{year}-{month:02d}-{day:02d}",
                                      "end":   f"{year}-{month:02d}-{day:02d}" }}
                }
            )
            continue

        if startH == 0:
            startH = 24
        
        if endH == 0:
            endH = 24

        create_task(
            title,
            datetime(year, month, day, startH-1, startM, 0),
            datetime(year, month, day, endH-1, endM, 0)
        )
    

if __name__ == "__main__":
    
    while True:
        print("This is happening!")
        pending = fetch_pending()

        for row in pending:

            page_id = row["id"]

            # Read blocks
            blocks = notion.blocks.children.list(block_id=page_id)["results"]
            text = "\n".join(t["plain_text"]
                             for block in blocks if block["type"] == "paragraph"
                             for t in block["paragraph"]["rich_text"])
            
            print(f"Text is : {text}")

            geminiResponse = gemini_chat(text)

            create_events(geminiResponse)
        
            notion.pages.update(
                page_id=page_id,
                properties={"Processed": {"checkbox": True}}
            )
        
        time.sleep(2)