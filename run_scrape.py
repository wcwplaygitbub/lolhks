import os
from apexlol_scraper import scrape_all_champions

cache_dir = os.path.join(os.path.dirname(__file__), "apexlol_cache")
scrape_all_champions(cache_dir)
print("Finished scraping all champions!")
