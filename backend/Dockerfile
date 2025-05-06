FROM python:3.11

WORKDIR /backend

COPY . .

RUN pip install --upgrade pip
RUN pip3 install --upgrade poetry==2.1.2

RUN python3 -m poetry config virtualenvs.create false \
    && python3 -m poetry install --no-interaction --no-ansi \
    && echo yes | python3 -m poetry cache clear . --all

CMD ["python", "main.py"]