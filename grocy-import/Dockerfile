ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache tesseract-ocr tesseract-ocr-data-eng poppler-utils

COPY app/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY run.sh /run.sh
COPY app/ /app/

RUN chmod a+x /run.sh

CMD ["/run.sh"]
