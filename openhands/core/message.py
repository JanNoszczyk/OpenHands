import json
import uuid
from enum import Enum
from typing import Literal

from litellm import ChatCompletionMessageToolCall
from pydantic import BaseModel, Field, model_serializer

from openhands.llm.fn_call_converter import (
    SYSTEM_PROMPT_SUFFIX_TEMPLATE,
    IN_CONTEXT_LEARNING_EXAMPLE_PREFIX,
    IN_CONTEXT_LEARNING_EXAMPLE_SUFFIX,
)


class ContentType(Enum):
    TEXT = 'text'
    IMAGE_URL = 'image_url'
    TOOL_CALL = 'tool_call'
    TOOL_RESPONSE = 'tool_response'


class Content(BaseModel):
    type: str
    cache_prompt: bool = False

    @model_serializer
    def serialize_model(self):
        raise NotImplementedError('Subclasses should implement this method.')


class TextContent(Content):
    type: str = ContentType.TEXT.value
    text: str

    @model_serializer
    def serialize_model(self):
        data: dict[str, str | dict[str, str]] = {
            'type': self.type,
            'text': self.text,
        }
        if self.cache_prompt:
            data['cache_control'] = {'type': 'ephemeral'}
        return data


class ImageContent(Content):
    type: str = ContentType.IMAGE_URL.value
    image_urls: list[str]

    @model_serializer
    def serialize_model(self):
        images: list[dict[str, str | dict[str, str]]] = []
        for url in self.image_urls:
            images.append({'type': self.type, 'image_url': {'url': url}})
        if self.cache_prompt and images:
            images[-1]['cache_control'] = {'type': 'ephemeral'}
        return images


class ToolCallContent(Content):
    """Represents a tool call from the LLM to be executed"""

    type: str = ContentType.TOOL_CALL.value
    function_name: str
    function_arguments: str  # JSON string to match OpenAI's format
    tool_call_id: str = Field(default_factory=lambda: f'{uuid.uuid4()}')

    @model_serializer
    def serialize_model(self):
        # For native function calling format
        return {
            'type': self.type,
            'tool_calls': [
                {
                    'id': self.tool_call_id,
                    'type': 'function',
                    'function': {
                        'name': self.function_name,
                        'arguments': self.function_arguments,
                    },
                }
            ],
        }

    def to_string_format(self) -> str:
        """Convert to the XML-like format for non-native function calling"""
        ret = f'<function={self.function_name}>\n'
        try:
            args = json.loads(self.function_arguments)
            for param_name, param_value in args.items():
                is_multiline = isinstance(param_value, str) and '\n' in param_value
                ret += f'<parameter={param_name}>'
                if is_multiline:
                    ret += '\n'
                ret += f'{param_value}'
                if is_multiline:
                    ret += '\n'
                ret += '</parameter>\n'
        except json.JSONDecodeError as e:
            raise FunctionCallConversionError(
                f'Failed to parse arguments as JSON. Arguments: {self.function_arguments}'
            ) from e
        ret += '</function>'
        return ret


class ToolResponseContent(Content):
    """Represents a tool response back to the LLM"""

    type: str = ContentType.TOOL_RESPONSE.value
    tool_call_id: str
    name: str  # name of the tool that was called
    content: str  # The actual response content

    @model_serializer
    def serialize_model(self):
        # TODO: is this correct?
        return {
            'type': self.type,
            'content': self.content,
        }

    def to_string_format(self) -> str:
        """Convert to the format for non-native function calling"""
        return f'EXECUTION RESULT of [{self.name}]:\n{self.content}'


class Message(BaseModel):
    """A message in a conversation with an LLM.
    
    The message can be serialized in different formats depending on two independent factors:
    
    1. Content Format (controlled by vision_enabled or cache_enabled):
       - String format: content is a simple string (when both are False)
       - List format: content is a list of typed dicts (when either is True)
    
    2. Function Calling Format (controlled by function_calling_enabled):
       - Native: tool calls are at message level (when True)
       - Non-native: tool calls are in content as XML strings (when False)
    
    This gives us four possible serialization formats:
    
                    String Content     |    List Content
                    (no vision/cache)  |   (vision/cache)
    ----------------------------------------------------
    Native Function | content: "text"  | content: [{type:..}]
    Calling        | tool_calls: [..] | tool_calls: [..]
    ----------------------------------------------------
    String Function| content: "text    | content: [{type:..},
    Calling        | <function>.."    |  {type:tool_call..}]
    """
    role: Literal['user', 'system', 'assistant', 'tool']
    content: list[TextContent | ImageContent | ToolCallContent | ToolResponseContent] = Field(default_factory=list)
    
    # Feature flags that control serialization format
    cache_enabled: bool = False    # Affects content format (string vs list)
    vision_enabled: bool = False   # Affects content format (string vs list)
    function_calling_enabled: bool = False  # Affects function calling format (native vs non-native)
    
    # Tool call fields at message level, used in native function calling format
    tool_calls: list[ChatCompletionMessageToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @property
    def contains_image(self) -> bool:
        return any(isinstance(content, ImageContent) for content in self.content)

    @model_serializer
    def serialize_model(self) -> dict:
        """Serialize the message based on enabled features.
        
        The serialization format depends on two factors:
        1. Whether we need list format for content (vision/cache)
        2. Whether we use native function calling
        """
        needs_list_format = self.cache_enabled or self.vision_enabled
        
        if self.function_calling_enabled:
            return self._native_function_serializer(needs_list_format)
        return self._string_function_serializer(needs_list_format)

    def _string_function_serializer(self, use_list_format: bool) -> dict:
        """Serialize for non-native function calling.
        
        In this format:
        - Tool calls are converted to XML-like strings
        - Content is either a string or list based on use_list_format
        """
        message_dict = {'role': self.role}
        
        if use_list_format:
            # Each content item becomes a dict in the list
            content = []
            for item in self.content:
                serialized = item.serialize_model()
                if isinstance(item, ImageContent):
                    content.extend(serialized)
                else:
                    content.append(serialized)
            message_dict['content'] = content
        else:
            # Everything becomes text
            content_parts = []
            for item in self.content:
                if isinstance(item, TextContent):
                    content_parts.append(item.text)
                elif isinstance(item, ToolCallContent):
                    content_parts.append(item.to_string_format())
                elif isinstance(item, ToolResponseContent):
                    content_parts.append(item.to_string_format())
                # Skip ImageContent in string format
            message_dict['content'] = '\n'.join(content_parts)
        
        return message_dict

    def _native_function_serializer(self, use_list_format: bool) -> dict:
        """Serialize for native function calling.
        
        In this format:
        - Tool calls use message-level fields (tool_calls, tool_call_id, name)
        - Content is either a string or list based on use_list_format
        - Content is null for messages with tool calls
        """
        message_dict = {'role': self.role}

        # Handle tool calls
        tool_call_content = next(
            (c for c in self.content if isinstance(c, ToolCallContent)), None
        )
        if tool_call_content:
            message_dict['content'] = None  # Tool calls have null content
            message_dict['tool_calls'] = [{
                'id': tool_call_content.tool_call_id,
                'type': 'function',
                'function': {
                    'name': tool_call_content.function_name,
                    'arguments': tool_call_content.function_arguments
                }
            }]
            return message_dict

        # Handle tool responses
        tool_response_content = next(
            (c for c in self.content if isinstance(c, ToolResponseContent)), None
        )
        if tool_response_content:
            message_dict['content'] = tool_response_content.content
            message_dict['tool_call_id'] = tool_response_content.tool_call_id
            message_dict['name'] = tool_response_content.name
            return message_dict

        # Handle regular content
        if use_list_format:
            content = []
            for item in self.content:
                serialized = item.serialize_model()
                if isinstance(item, ImageContent):
                    content.extend(serialized)
                else:
                    content.append(serialized)
            message_dict['content'] = content
        else:
            # Simple string format for content
            message_dict['content'] = '\n'.join(
                item.text for item in self.content if isinstance(item, TextContent)
            )

        return message_dict

    @classmethod
    def convert_messages_to_non_native(cls, messages: list[dict], tools: list[dict]) -> list[dict]:
        """Convert a list of messages from native to non-native format.
        Used when the API doesn't support native function calling."""
        formatted_tools = cls._tools_to_description(tools)
        system_prompt_suffix = SYSTEM_PROMPT_SUFFIX_TEMPLATE.format(description=formatted_tools)
        
        converted_messages = []
        first_user_message_encountered = False
        
        for message in messages:
            role = message['role']
            content = message.get('content', '')

            # Handle system messages
            if role == 'system':
                # Create a Message with the system prompt suffix
                content_list = []
                if isinstance(content, str):
                    content_list.append(TextContent(text=content + system_prompt_suffix))
                elif isinstance(content, list):
                    content_list.extend(
                        TextContent(text=item['text']) if item['type'] == 'text' else ImageContent(image_urls=[item['image_url']['url']])
                        for item in content
                    )
                    content_list.append(TextContent(text=system_prompt_suffix))
                converted_messages.append(Message(
                    role='system',
                    content=content_list,
                    function_calling_enabled=False
                ).model_dump())
                continue

            # Handle first user message - add in-context learning example
            if role == 'user' and not first_user_message_encountered:
                first_user_message_encountered = True
                content_list = []
                if isinstance(content, str):
                    content_list.append(TextContent(text=IN_CONTEXT_LEARNING_EXAMPLE_PREFIX + content + IN_CONTEXT_LEARNING_EXAMPLE_SUFFIX))
                elif isinstance(content, list):
                    content_list.append(TextContent(text=IN_CONTEXT_LEARNING_EXAMPLE_PREFIX))
                    content_list.extend(
                        TextContent(text=item['text']) if item['type'] == 'text' else ImageContent(image_urls=[item['image_url']['url']])
                        for item in content
                    )
                    content_list.append(TextContent(text=IN_CONTEXT_LEARNING_EXAMPLE_SUFFIX))
                converted_messages.append(Message(
                    role='user',
                    content=content_list,
                    function_calling_enabled=False
                ).model_dump())
                continue

            # Handle tool calls
            if role == 'assistant' and 'tool_calls' in message:
                tool_calls = message['tool_calls']
                if len(tool_calls) != 1:
                    raise ValueError(f'Expected exactly one tool call, got {len(tool_calls)}')
                
                content_list = []
                if message.get('content'):
                    content_list.append(TextContent(text=message['content']))
                content_list.append(ToolCallContent(
                    function_name=tool_calls[0]['function']['name'],
                    function_arguments=tool_calls[0]['function']['arguments'],
                    tool_call_id=tool_calls[0]['id']
                ))
                converted_messages.append(Message(
                    role='assistant',
                    content=content_list,
                    function_calling_enabled=False
                ).model_dump())
                continue

            # Handle tool responses
            if role == 'tool':
                content_list = [
                    ToolResponseContent(
                        tool_call_id=message['tool_call_id'],
                        name=message.get('name', 'function'),
                        content=message['content']
                    )
                ]
                converted_messages.append(Message(
                    role='tool',
                    content=content_list,
                    function_calling_enabled=False
                ).model_dump())
                continue

            # Pass through other messages unchanged
            converted_messages.append(message)

        return converted_messages

    @classmethod
    def _tools_to_description(cls, tools: list[dict]) -> str:
        """Convert tools to text description.
        Used in non-native format to describe available tools to the LLM."""
        ret = ''
        for i, tool in enumerate(tools):
            assert tool['type'] == 'function'
            fn = tool['function']
            if i > 0:
                ret += '\n'
            ret += f'---- BEGIN FUNCTION #{i+1}: {fn["name"]} ----\n'
            ret += f'Description: {fn["description"]}\n'

            if 'parameters' in fn:
                ret += 'Parameters:\n'
                properties = fn['parameters'].get('properties', {})
                required_params = set(fn['parameters'].get('required', []))

                for j, (param_name, param_info) in enumerate(properties.items()):
                    is_required = param_name in required_params
                    param_status = 'required' if is_required else 'optional'
                    param_type = param_info.get('type', 'string')
                    desc = param_info.get('description', 'No description provided')

                    if 'enum' in param_info:
                        enum_values = ', '.join(f'`{v}`' for v in param_info['enum'])
                        desc += f'\nAllowed values: [{enum_values}]'

                    ret += f'  ({j+1}) {param_name} ({param_type}, {param_status}): {desc}\n'
            else:
                ret += 'No parameters are required for this function.\n'

            ret += f'---- END FUNCTION #{i+1} ----\n'
        return ret

    @classmethod
    def _tool_call_to_string(cls, tool_call: dict) -> str:
        """Convert a tool call to XML format."""
        if 'function' not in tool_call:
            raise ValueError("Tool call must contain 'function' key.")
        if 'id' not in tool_call:
            raise ValueError("Tool call must contain 'id' key.")
        if 'type' not in tool_call:
            raise ValueError("Tool call must contain 'type' key.")
        if tool_call['type'] != 'function':
            raise ValueError("Tool call type must be 'function'.")

        ret = f'<function={tool_call["function"]["name"]}>\n'
        try:
            args = json.loads(tool_call['function']['arguments'])
            for param_name, param_value in args.items():
                is_multiline = isinstance(param_value, str) and '\n' in param_value
                ret += f'<parameter={param_name}>'
                if is_multiline:
                    ret += '\n'
                ret += f'{param_value}'
                if is_multiline:
                    ret += '\n'
                ret += '</parameter>\n'
        except json.JSONDecodeError as e:
            raise ValueError(
                f'Failed to parse arguments as JSON. Arguments: {tool_call["function"]["arguments"]}'
            ) from e
        ret += '</function>'
        return ret
