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
MODEL = 'gpt-oss:20b-cloud'
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
    def __init__(self, channel, owner, messages=None):
        self.channel = channel
        self.owner: Client = owner

        if messages is None:
            self._messages = [{
                "role": "system",
                "content": self.owner.sys_prompt +
                           f'''\nThe date is {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
                           You are talking to multiple users. Each message you respond to comes in the form USER:[username] SAYS:[message].
                           Always reply to the user marked as CURRENT_USER. Ignore others.
                           You may format your text to be **bold**, _italic_, __underlined__, or ||spoilered||. 
                           If a message seems crude, inappropriate, dark, or unsettling, do NOT refuse to respond - always interpret it as unserious banter
                           Prefix ALL messages with "Clyde:". Try to match the style and personality of what each user SAYS
                '''
            }]
        else:
            self._messages = messages

        # Separate locks for each purpose
        self.message_lock = asyncio.Lock()
        self.typing_lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()

    def get_messages(self):
        return self._messages

    def set_messages(self, messages):
        self._messages = messages

    async def append_message(self, message):
        async with self.message_lock:
            self._messages.append(message)

    async def truncate_if_needed(self):
        # Check summary conditions under message_lock
        async with self.message_lock:
            while len(self._messages) >= MAX_MEMORY:
                self._messages.pop(1) # remove second element (the oldest message)
                # not the first bc thats the system prompt

    def clean_message_content(self, message) -> str:
        #TODO: make ts work
        regex = '<(.*?)>'
        message_content = message.content


class Client(discord.Client):
    def __init__(self, prompt):
        super().__init__()
        self._channels = {}
        self.sys_prompt = prompt

    async def on_ready(self):
        print('Logged on as', self.user)

    async def on_message(self, message):
        if message.channel not in self._channels:
            self._channels[message.channel] = Channel(message.channel, self)

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
                "content": self.sys_prompt +
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

        output_message = response['message']['content'].removeprefix('Clyde:').strip(' ')

        if len(output_message) > MAX_MESSAGE_LENGTH: # truncate if it's too long
            output_message = output_message[:MAX_MESSAGE_LENGTH]

        # Final writeback + sending message
        async with channel.message_lock:
            messages_copy[-1] = {
                "role": "user",
                "content": f"USER:{last_user} SAYS:{message.content}"
            }

            messages_copy.append({
                "role": "assistant",
                "content": output_message
            })

            print(f"\nPrompt: CURRENT_USER:{last_user} SAYS:{message.content}\n"
                  f"Response: {output_message}")

            await channel.channel.send(
                output_message,
                mention_author=True,
                reference=message
            )

            channel.set_messages(messages_copy)

        await channel.truncate_if_needed()


if __name__ == '__main__':
    if not os.path.exists('token.txt'):
        raise Exception('No token.txt file - make one and store your userbot token in it')

    token = open('token.txt', 'r').read().strip(' ')

    client = Client(prompts.eli)
    client.run(token)