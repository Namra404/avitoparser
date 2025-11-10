import asyncio
import datetime
import json
import time

import aiohttp
import requests
from aiohttp import ClientSession


result = []
file_name = 'result.json'

def write_parsed_vacancies_in_json(file_name:str, vacancies: list):
    with open(file_name, 'w', encoding='utf-8') as f:
        json.dump(vacancies, f, ensure_ascii=False, indent=4)
        print("Успешно записал страницы")


async def get_page_hh_data(session: ClientSession, page):
    url = "https://api.hh.ru/vacancies"
    params = {"text": "python junior", 'per_page': 100, 'page': page}
    async with session.get(url=url, params=params) as response:
        response.raise_for_status()
        res = (await response.json())['items']
        result.extend(res)
        print(f"Успешно спарсил страницу {page}")


async def gather_data():
    async with aiohttp.ClientSession() as session:
        page = 0
        url = "https://api.hh.ru/vacancies"
        params = {"text": "python junior", 'per_page': 100, 'page': page}
        async with session.get(url=url, params=params) as response:
            response.raise_for_status()
            total_pages = (await response.json())['pages']
        tasks = []
        for page in range(total_pages):
            task = asyncio.create_task(get_page_hh_data(session= session, page = page))
            tasks.append(task)
        await asyncio.gather(*tasks)

def main():
    start_time = time.time()
    asyncio.run(gather_data())
    end_time = time.time()
    print(f"Спарсил за {end_time - start_time} секунд")
    write_parsed_vacancies_in_json(file_name=file_name, vacancies=result)

if __name__ == "__main__":
    main()


