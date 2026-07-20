from announcement import update, init_db
from datetime import datetime

TODAY = datetime.today().strftime("%Y%m%d")
print(f"Fetching announcements for {TODAY}...")
count = update(from_date=TODAY, to_date=TODAY)
print(f"Inserted {count} new announcements")
