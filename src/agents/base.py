import asyncio
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    def __init__(self, config):
        self.config = config

    @abstractmethod
    async def run(self, input_queue: asyncio.Queue, output_queue: asyncio.Queue):
        ...
