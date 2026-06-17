ARG BUILD_FROM
FROM $BUILD_FROM

COPY app/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh
RUN wget -q https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js -O /app/chart.umd.min.js

CMD [ "/run.sh" ]
