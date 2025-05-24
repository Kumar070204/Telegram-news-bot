import requests
from bs4 import BeautifulSoup

url = "https://www.bbc.com/news"
print(f"Scraping {url}...")
response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
soup = BeautifulSoup(response.text, 'html.parser')
elements = soup.find_all('h3', class_='gs-c-promo-heading__title')
for i, h in enumerate(elements[:5], 1):
    headline = h.text.strip()
    link = h.find_parent('a')['href']
    print(f"{i}. {headline}: {link}")
print(f"Found {len(elements)} headlines")