import os
from time import sleep
from concurrent.futures import ThreadPoolExecutor
import re
import requests
import subprocess
import datetime
import asyncio
import discord
import ollama
import prompts

MAX_MEMORY = 100
MAX_MESSAGE_LENGTH = 2000
MAX_SIMULTANEOUS_PROMPTS = 4
SUMMARISE_CONVERSATIONS = True
MODEL = 'gpt-oss:20b-cloud'
SUMMARY_MODEL = 'qwen3:0.6b'
TOKEN = '' # enter your token here

system_prompt = prompts.six_seven_prompt
summariser_prompt = ("Your purpose is summarising a provided conversation. Keep it short - one or two sentences."
                     "Begin with 'The conversation so far has been about '")

summariser_message = [
    {"role": "system", "content": summariser_prompt}
]

executor = ThreadPoolExecutor()

async def get_response(messages_list, model):
    output = { 'message' : {'content' : ''}}

    while output['message']['content'] == '':
        try:
            loop = asyncio.get_running_loop()
            output = await loop.run_in_executor(
                executor,
                lambda: ollama.chat(model=model, messages=messages_list, think=False)
            )
        except ConnectionError:
            start_ollama()

    return output


def make_summary_payload(messages_to_summarise):
    content = "\n".join(
        f"{m['content']}" for m in messages_to_summarise if m['role'] == 'user'
    )
    return [
        {"role": "system", "content": summariser_prompt},
        {"role": "user", "content": content}
    ]


def ollama_running() -> bool:
    try:
        requests.get("http://localhost:11434", timeout=0.5)
        return True
    except requests.exceptions.ConnectionError:
        return False


def start_ollama():
    print('Starting ollama...')
    subprocess.Popen(['ollama', 'serve'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for i in range(20):
        if ollama_running():
            print('Ollama is running')
            return

        sleep(0.5)

    raise RuntimeError('Failed to start ollama')


def multiple_instances_running(message, messages_list) -> bool:
    """BAD
    dont use bc its broken the moment the llm pings itself
    """
    for message_element in messages_list:
        if message.content == message_element['content']:
            return False

    return True


class Channel:
    def __init__(self, channel, messages=None):
        self.channel = channel

        if messages is None:
            self._messages = [{
                "role": "system",
                "content": system_prompt +
                           f'\nThe date is {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}'
            }]
        else:
            self._messages = messages

        # Separate locks for each purpose
        self.message_lock = asyncio.Lock()
        self.typing_lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()

        self._summarising = False

    def get_messages(self):
        return self._messages

    def set_messages(self, messages):
        self._messages = messages

    async def append_message(self, message):
        async with self.message_lock:
            self._messages.append(message)

    async def summarise_if_needed(self):
        # Check summary conditions under message_lock
        async with self.message_lock:
            if self._summarising:
                return
            if len(self._messages) <= MAX_MEMORY or not SUMMARISE_CONVERSATIONS:
                return
            self._summarising = True

        # Summarisation outside the lock
        summary_payload = make_summary_payload(self._messages[1:])
        summary = await get_response(summary_payload, SUMMARY_MODEL)

        # Write summary back under message_lock
        async with self.message_lock:
            self._messages = [
                {"role": "system",
                 "content": system_prompt +
                            f'\nThe date is {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}'},
                {"role": "system", "content": summary['message']['content']}
            ]
            self._summarising = False
            print(f"SUMMARISED: {summary['message']['content']}")

    def clean_message_content(self, message) -> str:
        #TODO: make ts work
        regex = '<(.*?)>'
        message_content = message.content


class MyClient(discord.Client):
    def __init__(self):
        super().__init__()
        self._channels = {}

    async def on_ready(self):
        print('Logged on as', self.user)

    async def on_message(self, message):
        if message.channel not in self._channels:
            self._channels[message.channel] = Channel(message.channel)

        channel: Channel = self._channels[message.channel]

        if ((not self.user.mentioned_in(message)
             and not 'Direct Message' in str(channel.channel))
                or len(str(message.content)) > MAX_MESSAGE_LENGTH):
            return

        if message.author == self.user:
            return

        last_user = message.author.name

        #clean_message = Channel.clean_message_content(message.content)

        # Copy messages WITHOUT holding any locks
        messages_copy = channel.get_messages().copy()

        # Update system message + add CURRENT_USER line
        async with channel.message_lock:
            messages_copy[0] = {
                "role": "system",
                "content": system_prompt +
                           f'\nThe date is {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}'
            }
            messages_copy.append({
                "role": "user",
                "content": f"CURRENT_USER:{last_user} SAYS:{message.content}"
            })

        response = {}

        # LLM call: use model_lock + typing_lock
        async with channel.model_lock:
            async with channel.typing_lock:
                async with channel.channel.typing():
                    response = await get_response(messages_copy, MODEL)

        # Final writeback + sending message
        async with channel.message_lock:
            messages_copy[-1] = {
                "role": "user",
                "content": f"USER:{last_user} SAYS:{message.content}"
            }

            messages_copy.append({
                "role": "assistant",
                "content": response['message']['content']
            })

            output_message = response['message']['content'].removeprefix('Clyde:').strip(' ')

            print(f"\nPrompt: CURRENT_USER:{last_user} SAYS:{message.content}\n"
                  f"Response: {output_message}")

            await channel.channel.send(
                output_message,
                mention_author=True,
                reference=message
            )

            channel.set_messages(messages_copy)

        await channel.summarise_if_needed()


if __name__ == '__main__':
    if not os.path.exists('token.txt'):
        raise Exception('No token.txt file - make one and store your userbot token in it')

    token = open('token.txt', 'r').read().strip(' ')

    client = MyClient()
    client.run(token)