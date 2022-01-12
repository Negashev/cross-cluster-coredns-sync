FROM python:3-alpine

WORKDIR /src

ADD requirements.txt ./

RUN pip3 --no-cache install -r requirements.txt

CMD ["python3", "-u", "run.py"]

ADD *.py /src/