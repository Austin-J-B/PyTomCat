import json
import os
from typing import Dict, List
from ..config import settings

# Default to EMPTY structure so it doesn't autopopulate names.
# This matches your request to "Create" the schedule, not have one forced on you.
DEFAULT_SCHEDULE = {
    "Microwave":         ["", "", "", "", "", "", ""],
    "Snickers":          ["", "", "", "", "", "", ""],
    "Business":          ["", "", "", "", "", "", ""],
    "The Greens":        ["", "", "", "", "", "", ""],
    "HOP":               ["", "", "", "", "", "", ""],
    "Lot 50":            ["", "", "", "", "", "", ""],
    "Mary Kay and Zen":  ["", "", "", "", "", "", ""],
    "West Hall":         ["", "", "", "", "", "", ""],
    "Maintenance":       ["", "", "", "", "", "", ""],
}

def _ensure_file_exists():
    path = settings.schedule_file
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_SCHEDULE, f, indent=2)

def load_schedule() -> Dict[str, List[str]]:
    _ensure_file_exists()
    try:
        with open(settings.schedule_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading schedule: {e}")
        return DEFAULT_SCHEDULE

def save_schedule(data: Dict[str, List[str]]) -> bool:
    _ensure_file_exists()
    try:
        with open(settings.schedule_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving schedule: {e}")
        return False