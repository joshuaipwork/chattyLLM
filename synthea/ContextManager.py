# -*- coding: utf-8 -*-
"""
Generate a prompt for the AI to respond to, given the
message history and persona.
"""
from typing import AsyncIterator, Optional
import discord
import pypdf
from jinja2 import Environment
import os
import yaml

from synthea.CommandParser import ChatbotParser, CommandError, ParsedArgs, ParserExitedException
from synthea import SyntheaClient
from synthea.Config import Config

class ReplyChainIterator:
    """
    An async iterator which follows a chain of discord message replies until it reaches the end
    or fails to capture the last message.
    """

    def __init__(self, starting_message: discord.Message):
        self.message = starting_message
        self.message_index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.message_index == 0:
            self.message_index += 1
            return self.message            
        # go back message-by-message through the reply chain and add it to the context
        if self.message.reference:
            self.message_index += 1
            try:
                self.message = await self.message.channel.fetch_message(
                    self.message.reference.message_id
                )
                return self.message

            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                # the user may have deleted their message
                # either way, we can't follow the history anymore
                # pylint: disable-next=raise-missing-from
                raise StopAsyncIteration
        else:
            raise StopAsyncIteration


class ContextManager:
    """
    Formats prompts for the bot to generate from.
    """

    # A rough measure of how many character are in each token.
    EST_CHARS_PER_TOKEN = 3

    def __init__(self, bot_user_id: int):
        """
        model (str): The model that is generating the text. Used to determine the prompt format
            and other configuration options.
        bot_user_id (str): The discord user id of the bot. Used to determine if a message came from
            the bot or from a user.
        """
        self.parser = ChatbotParser()
        self.bot_user_id: int = bot_user_id

    async def generate_chat_history_from_chat(
        self, message: discord.Message, system_prompt: Optional[str] = None
    ) -> tuple[list[dict[str, str]], ParsedArgs]:
        """
        Generates a prompt which includes the context from previous messages in a reply chain.
        Messages outside of the reply chain are ignored.

        Args:
            message (discord.Message): The last message from the user.
            system_prompt (str): The system prompt to use when generating the prompt
        """
        history_iterator: ReplyChainIterator = ReplyChainIterator(message)
        chat_history, args = await self.compile_chat_history(
            message=message,
            history_iterator=history_iterator,
            default_system_prompt=system_prompt,
        )

        return chat_history, args


    async def convert_chat_history_to_prompt(self, chat_history: list[dict[str, str]], chat_template: str) -> str:
        """
        Takes a chat template and converts it to a prompt. 

        Args:
            chat_history (list of dict of str to str): A list of chat messages to convert to a prompt.
                Refer to huggingface's chat template feature for information on how this should be formatted.
            chat_template (str): The jinja2 chat template to apply to the chat history.
        """
        chat_template = "{% for message in messages %}{% if message['role'] == 'user' %}{{ '### Instruction:\\n' + message['content'].strip()}}{% elif message['role'] == 'system' %}{{ message['content'].strip() }}{% elif message['role'] == 'assistant' %}{{ '### Response\\n'  + message['content'] }}{% endif %}{{'\\n\\n'}}{% endfor %}{{ '### Response:\\n' }}"

        # Create a Jinja2 environment and compile the template
        env = Environment()
        template = env.from_string(chat_template)

        # Render the template with your messages
        formatted_chat = template.render(messages=chat_history)

        return formatted_chat

    async def compile_chat_history(
        self,
        message: discord.Message,
        history_iterator: AsyncIterator[discord.Message],
        default_system_prompt: Optional[str] = None,
    ) -> tuple[list[dict[str, str]], ParsedArgs]:
        """
        Generates a prompt which includes the context from previous messages from the history.
        Returns the command which applies to this chat history, which is the last command which
        was sent in the reply chain.

        Args:
            message (discord.Message): The last message to add to the prompt.
            history_iterator (ReplyChainIterator): An iterator that contains the chat history
                to be included in the prompt.
            default_system_prompt (str): The system prompt to use if the system prompt is not   
                overriden by another command within the chat history
        """
        config = Config()

        # pieces of the prompts are appended to the list then assembled in reverse order into the final prompt
        token_count: int = 0
        args: ParsedArgs | None = None
        system_prompt = None

        # use provided system prompt
        messages = []

        # retrieve as many tokens as can fit into the context length from history
        history_token_limit: int = config.context_length - config.max_new_tokens
        system_prompt_tokens: int = len(default_system_prompt) // self.EST_CHARS_PER_TOKEN
        token_count += system_prompt_tokens
        async for message in history_iterator:
            # some messages in the chain may be commands for the bot
            # if so, parse only the prompt in each command in order to not confuse the bot
            if message.clean_content.lower().startswith(config.command_start_str.lower()):
                message_args: ParsedArgs = self.parser.parse(message.clean_content)
                if not args:
                    args = message_args
                if not system_prompt and args.use_as_system_prompt:
                    system_prompt = args.prompt
                    continue
            text, added_tokens = await self._get_text(message, history_token_limit - token_count, config)

            # # 
            if not args and message.author.id == self.bot_user_id:
                # bot uses embeds to speak as a character
                full_message: discord.Message = await message.channel.fetch_message(
                    message.id
                )
                # if no embed, it wasn't speaking as a character
                if full_message.embeds:
                    embed = full_message.embeds[0]
                    if embed.footer.text == SyntheaClient.SYSTEM_TAG:
                        continue

            # don't include empty messages so the bot doesn't get confused.
            if not text:
                continue

            # stop retrieving context if the context would overflow
            if added_tokens + token_count > history_token_limit:
                break

            # update the prompt with this message
            if message.author.id == self.bot_user_id:
                messages.insert(0, {"role": "assistant", "content": text})
            else:
                messages.insert(0, {"role": "user", "content": f"Message from {message.author.display_name} \n {text}"})
            
            token_count += added_tokens

        # add the system prompt
        messages.insert(0, {"role": "system", "content": system_prompt if system_prompt else default_system_prompt})

        return messages, args
    
    async def read_attachment(self, message: discord.Attachment):
        attachment_string = ""
        attachment_bytes = await message.read()
        if not message.content_type or message.content_type.startswith("text/"):
            attachment_string = attachment_bytes.decode()
        elif "application/pdf" in message.content_type:
            print("Saving the pdf attachment")
            await message.save(message.filename)
            reader = pypdf.PdfReader(message.filename)

            print(f"Found {len(reader.pages)} pages in PDF. Reading them.")
            for page in reader.pages:
                page_text = page.extract_text()
                attachment_string = attachment_string + "\n" + page_text
            
            print("Removing the saved file")
            os.remove(message.filename)

        print(f"Obtained the text from the [{message.content_type}] attachment as a string")
        print(f"{attachment_string}")
        return attachment_string

    async def _get_text(self, message: discord.Message, remaining_tokens: int, config: Config) -> tuple[str, int]:
        """
        Gets the text from a message and counts the tokens.

        Under most conditions, the text it returns will be message.content, however if it is a command
        for the bot, then only the prompt from that command will be returned.
        """
        # when the bot plays characters, it stores text in embeds rather than content
        if message.author.id == self.bot_user_id and message.embeds:
            text = message.embeds[0].description
        # check if the message is a command. If so, only include the prompt from the command
        elif message.clean_content.lower().startswith(config.command_start_str.lower()):
            try:
                args = ChatbotParser().parse(message.clean_content)
                text = args.prompt
            except (CommandError, ParserExitedException):
                # if the command is invalid, just append the whole thing
                text = message.clean_content
        else:
            text = message.clean_content

        # Iterate through any attachments associated with the message
        for attachment in message.attachments:
            attachment_content = await self.read_attachment(attachment)
            if not attachment_content or attachment_content.isspace():
                text = text + "\n\nA file was attached to this message, but it is either empty or is not a file type you can read."
            elif (len(attachment_content) // self.EST_CHARS_PER_TOKEN > remaining_tokens):
                text = text + "\n\nA file was attached to this message, but it was too large to be read."
            else:
                text = text + "\n\nA file was attached to this message. Here are the contents:\n" + attachment_content

        tokens = len(text) // self.EST_CHARS_PER_TOKEN
        return text, tokens
