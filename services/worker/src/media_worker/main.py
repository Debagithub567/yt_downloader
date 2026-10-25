from redis import Redis
from rq import Queue, Worker

from media_core.settings import Settings


def main() -> None:
    settings = Settings()
    connection = Redis.from_url(settings.redis_url)
    queue = Queue("media", connection=connection)
    Worker([queue], connection=connection).work()


if __name__ == "__main__":
    main()

