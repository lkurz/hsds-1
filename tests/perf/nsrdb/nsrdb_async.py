import aiohttp
import asyncio
import sys
import random
import base64
import logging
import time
import numpy as np
from config import Config

BUCKET_NAME = "nrel-pds-hsds" 
DOMAIN = "/nrel/nsrdb/v3/nsrdb_2000.h5" 
H5_PATH = "/wind_speed"
DEFAULT_BLOCK_SIZE = 1000
NUM_COLS = 17568
NUM_ROWS = 2018392
DSET_ID = "d-096b7930-5dc5b556-dbc8-00c5ad-8aca89"  # wind-speed dataset
DSET_TYPE = 'i2'  # two-byte signed integer
NUM_RETRIES = 10
SLEEP_TIME = 0.1

cfg = Config()

class DataFetcher:
    def __init__(self, app, max_tasks=10):
        self._app = app
          
        logging.info(f"DataFetcher.__init__  {self.bucket}/{self.domain} {self.dsetid}")
        self._app = app
        self._q = asyncio.Queue()
        num_blocks = -(-NUM_ROWS // self.block_size)  # integer ceiling
        for i in range(num_blocks):
            self._q.put_nowait(i*self.block_size)

        if num_blocks < max_tasks:
            self._max_tasks = num_blocks
        else:  
            self._max_tasks = max_tasks


    def getHeaders(self, format="binary"):
        """Get default request headers for domain"""
        username = self.username
        password = self.password  
        headers = {}
        if username and password:
            auth_string = username + ':' + password
            auth_string = auth_string.encode('utf-8')
            auth_string = base64.b64encode(auth_string)
            auth_string = "Basic " + auth_string.decode("utf-8")
            headers['Authorization'] = auth_string

        if format == "binary":
            headers["accept"] = "application/octet-stream"

        return headers

    @property
    def domain(self):
        return self._app["domain"]

    @property
    def dsetid(self):
        return self._app["dsetid"]

    @property
    def dsettype(self):
        return self._app["dsettype"]

    @property
    def bucket(self):
        return self._app["bucket"]

    @property
    def verbose(self):
        return self._app["verbose"]

    @property
    def block_size(self):
        return self._app["block_size"]

    @property
    def num_rows(self):
        return self._app["num_rows"]

    @property
    def index(self):
        return self._app["index"]

    @property
    def username(self):
        return self._app["hs_username"]
           
    @property
    def password(self):
        return self._app["hs_password"]
         
    @property
    def endpoint(self):
        return self._app["hs_endpoint"]

    @property
    def result(self):
        return self._app["result"]

    @property
    def retries(self):
        return self._app["retries"]

    @property
    def sleep_time(self):
        return self._app["sleep_time"]
         

    async def fetch(self):
        workers = [asyncio.Task(self.work())
            for _ in range(self._max_tasks)]
        # When all work is done, exit.
        msg = f"DataFetcher max_tasks {self._max_tasks} = await queue.join "
        logging.info(msg)
        await self._q.join()
        msg = "DataFetcher - join complete"
        logging.info(msg)

        for w in workers:
            w.cancel()
        logging.debug("DataFetcher - workers canceled")

    async def work(self):
        max_retries = self.retries
        async with aiohttp.ClientSession() as session:
            block = await self._q.get()
            retry_count = 0
            sleep_time = self.sleep_time

            while True:
                start_ts = time.time()
                status_code = None
                try:
                    status_code = await self.read_block(session, block)
                except IOError as ioe:
                    logging.error(f"got IOError: {ioe}")
                elapsed = time.time() - start_ts
                msg = f"DataFetcher - task {block} start: {start_ts:.3f} "
                msg += f"elapsed: {elapsed:.3f}"
                if status_code == 200:
                    self._q.task_done()
                    block = await self._q.get()
                    retry_count = 0
                    sleep_time = self.sleep_time

                elif status_code == 503:
                    logging.warning(f"server too busy, sleeping for {sleep_time}")
                    await asyncio.sleep(sleep_time)
                    sleep_time *= 2  # wait twice as long next time
                else:
                    logging.error(f"got status code: {status_code} retry: {retry_count}")
                    retry_count += 1
                    if retry_count > max_retries:
                        # move on to another block
                        self._q.task_done()
                        block = await self._q.get()
                        retry_count = 0
                        sleep_time = self.sleep_time

                logging.info(msg)

    async def read_block(self, session, block):
        row_start = block
        row_end = block + self.block_size
        num_rows = row_end - row_start
        index = self.index
        dt = np.dtype(self.dsettype)
        if row_end > self.num_rows:
            row_end = self._num_rows
        expected_bytes = num_rows * dt.itemsize

        headers = self.getHeaders()
        req = f"{self.endpoint}/datasets/{self.dsetid}/value"
        
        select = f"[{index},{row_start}:{row_end}]"
        params = {}
        params["select"] = select
        params["domain"] = self.domain
        params["bucket"] = self.bucket
        logging.debug(f"read_block({block}): sending req: {req}, {select}")
        status_code = 500
        async with session.get(req, headers=headers, params=params) as rsp:
            if rsp.status == 200:
                if 'Content-Type' not in rsp.headers:
                    msg = "expected Content-Type is response headers"
                    logging.error(msg)
                    raise ValueError(msg)
                if rsp.headers['Content-Type'] != "application/octet-stream":
                    msg = "expected binary response"
                    logging.error(msg)
                    raise IOError(msg)
                data = await rsp.read() 
                if len(data) != expected_bytes:
                    msg = f"Expected {expected_bytes} but got: {len(data)}"
                    logging.error(msg)
                    raise IOError(msg)
                arr = np.frombuffer(data, dtype=dt)
                logging.debug(f"read_block({block}): got {arr.min()}, {arr.max()}, {arr.mean():4.2f}")
                result = self.result
                # slot in to result array
                result[row_start:row_end] = arr
            status_code =rsp.status
        return status_code
        
       

# parse command line args
index = None
block_size = None
max_tasks = 10
for narg in range(1, len(sys.argv)):
    arg = sys.argv[narg]
    if arg in ("-h", "--help"):
        print("usage: python nsrdb_async.py [--index=n] [--block==n] [--tasks=n]")
        sys.exit(0)
    if arg.startswith("--index="):
        index = int(arg[len("--index="):])
    elif arg.startswith("--block="):
        block_size = int(arg[len("--block="):])
    elif arg.startswith("--tasks="):
        max_tasks = int(arg[len("--tasks="):])
    
    else:
        print(f"unexpected argument: {arg}")
 
if index is None:
    # choose a random index
    index = random.randrange(0, NUM_COLS)
if block_size is None:
    # read entire column in one call
    block_size = DEFAULT_BLOCK_SIZE

loglevel = logging.WARNING
logging.basicConfig(format='%(asctime)s %(message)s', level=loglevel)
    
# init app dictionary
cfg["domain"] = DOMAIN
cfg["bucket"] = BUCKET_NAME
cfg["dsetid"] = DSET_ID
cfg["dsettype"] = DSET_TYPE
cfg["bucket_name"] = BUCKET_NAME
cfg["block_size"] = block_size
cfg["index"] = index
cfg["num_rows"] = NUM_ROWS
cfg["retries"] =  NUM_RETRIES
cfg["sleep_time"] = SLEEP_TIME

# array will be filled in by workers
result = np.zeros((NUM_ROWS,), dtype=np.dtype(DSET_TYPE))

cfg["result"] = result

data_fetcher = DataFetcher(cfg, max_tasks=max_tasks)
loop = asyncio.get_event_loop()
loop.run_until_complete(data_fetcher.fetch())
print(f"{H5_PATH}[{index}:]: {result}")
print(f"{result.min()}, {result.max()}, {result.mean():4.2f}")

 





