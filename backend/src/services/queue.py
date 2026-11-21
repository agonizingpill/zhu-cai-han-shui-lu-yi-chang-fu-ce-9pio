import os, redis, json
client = redis.Redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'), decode_responses=True)
QUEUE = 'bamboo:moisture-analysis'
COMPARISON_QUEUE = 'bamboo:baseline-comparison'


def put(job_id):
    client.rpush(QUEUE, json.dumps({'job_id': job_id}))


def put_comparison(run_id):
    client.rpush(COMPARISON_QUEUE, json.dumps({'run_id': run_id}))


def requeue_stale(queue_name):
    """把队列里卡住的消息重新入队（消息投递即续跑信号，重复投递由状态条件保证幂等）。"""
    backlog = []
    while True:
        item = client.lpop(queue_name)
        if item is None:
            break
        backlog.append(item)
    for item in backlog:
        client.rpush(queue_name, item)
    return len(backlog)
