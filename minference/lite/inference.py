#market_agents\inference\sql_inference.py
import asyncio
import json
from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field, ValidationError
from minference.lite.models import RawOutput, ProcessedOutput, ChatThread , LLMClient , ResponseFormat, CallableTool, StructuredTool
from minference.clients_models import AnthropicRequest, OpenAIRequest, VLLMRequest
from minference.lite.oai_parallel import process_api_requests_from_file, OAIApiFromFileConfig
import os
from dotenv import load_dotenv
import time
from uuid import UUID
from anthropic.types.message_create_params import ToolChoiceToolChoiceTool,ToolChoiceToolChoiceAuto
from minference.lite.models import Entity


class RequestLimits(Entity):
    """
    Configuration for API request limits.
    Inherits from Entity for UUID handling and registry integration.
    """
    max_requests_per_minute: int = Field(
        default=50,
        description="The maximum number of requests per minute for the API"
    )
    max_tokens_per_minute: int = Field(
        default=100000,
        description="The maximum number of tokens per minute for the API"
    )
    provider: Literal["openai", "anthropic", "vllm", "litellm"] = Field(
        default="openai",
        description="The provider of the API"
    )


class InferenceOrchestrator:
    def __init__(self, oai_request_limits: Optional[RequestLimits] = None, 
                 anthropic_request_limits: Optional[RequestLimits] = None, 
                 vllm_request_limits: Optional[RequestLimits] = None,
                 litellm_request_limits: Optional[RequestLimits] = None,
                 local_cache: bool = True,
                 cache_folder: Optional[str] = None):
        load_dotenv()
        self.openai_key = os.getenv("OPENAI_KEY")
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        self.vllm_key = os.getenv("VLLM_API_KEY")
        self.vllm_endpoint = os.getenv("VLLM_ENDPOINT", "http://localhost:8000/v1/chat/completions")
        self.litellm_endpoint = os.getenv("LITELLM_ENDPOINT", "http://localhost:8000/v1/chat/completions")
        self.litellm_key = os.getenv("LITELLM_API_KEY")
        
        # Create default request limits with provider-specific settings
        self.oai_request_limits = oai_request_limits or RequestLimits(
            max_requests_per_minute=500,
            max_tokens_per_minute=200000,
            provider="openai"
        )
        self.anthropic_request_limits = anthropic_request_limits or RequestLimits(
            max_requests_per_minute=50,
            max_tokens_per_minute=40000,
            provider="anthropic"
        )
        self.vllm_request_limits = vllm_request_limits or RequestLimits(
            max_requests_per_minute=500,
            max_tokens_per_minute=200000,
            provider="vllm"
        )
        self.litellm_request_limits = litellm_request_limits or RequestLimits(
            max_requests_per_minute=500,
            max_tokens_per_minute=200000,
            provider="litellm"
        )
        
        self.local_cache = local_cache
        self.cache_folder = self._setup_cache_folder(cache_folder)
        self.all_requests = []

    def _setup_cache_folder(self, cache_folder: Optional[str]) -> str:
        if cache_folder:
            full_path = os.path.abspath(cache_folder)
        else:
            # Go up two levels from the current file's directory to reach the project root
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
            full_path = os.path.join(repo_root, 'outputs', 'inference_cache')
        
        os.makedirs(full_path, exist_ok=True)
        return full_path
    
    def _create_chat_thread_hashmap(self, chat_threads: List[ChatThread]) -> Dict[UUID, ChatThread]:
        return {p.id: p for p in chat_threads if p.id is not None}
    
    def _update_chat_thread_history(self, chat_threads: List[ChatThread], llm_outputs: List[ProcessedOutput]) -> List[ChatThread]:
        chat_thread_hashmap = self._create_chat_thread_hashmap(chat_threads)
        for output in llm_outputs:
            if output.chat_thread_id:
                print(f"updating chat thread history for chat_thread_id: {output.chat_thread_id} with output: {output}")
                chat_thread_hashmap[output.chat_thread_id].add_chat_turn_history(output)
        return list(chat_thread_hashmap.values())
    
    

    async def run_parallel_ai_completion(self, chat_threads: List[ChatThread]) -> List[ProcessedOutput]:
        print("Running parallel AI completion")
        
        tasks = []
        if any(p for p in chat_threads if p.llm_config.client == "openai"):
            tasks.append(self._run_openai_completion([p for p in chat_threads if p.llm_config.client == "openai"]))
        if any(p for p in chat_threads if p.llm_config.client == "anthropic"):
            tasks.append(self._run_anthropic_completion([p for p in chat_threads if p.llm_config.client == "anthropic"]))
        if any(p for p in chat_threads if p.llm_config.client == "vllm"):
            tasks.append(self._run_vllm_completion([p for p in chat_threads if p.llm_config.client == "vllm"]))
        if any(p for p in chat_threads if p.llm_config.client == "litellm"):
            tasks.append(self._run_litellm_completion([p for p in chat_threads if p.llm_config.client == "litellm"]))

        results = await asyncio.gather(*tasks)
        flattened_results = [item for sublist in results for item in sublist]
        
        # Create new ProcessedOutputs with unique IDs
        processed_outputs = []
        for result in flattened_results:
            try:
                # Only create ProcessedOutput if result is RawOutput
                if isinstance(result, RawOutput):
                    processed_output = result.create_processed_output()
                    processed_outputs.append(processed_output)
                elif isinstance(result, ProcessedOutput):
                    processed_outputs.append(result)
            except Exception as e:
                print(f"Error processing result: {e}")
                continue

        return processed_outputs
        
    def get_all_requests(self):
        requests = self.all_requests
        self.all_requests = []  
        return requests

    async def _run_openai_completion(self, chat_threads: List[ChatThread]) -> List[ProcessedOutput]:
        
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        requests_file = os.path.join(self.cache_folder, f'openai_requests_{timestamp}.jsonl')
        results_file = os.path.join(self.cache_folder, f'openai_results_{timestamp}.jsonl')
        self._prepare_requests_file(chat_threads, "openai", requests_file)
        config = self._create_oai_completion_config(chat_threads[0], requests_file, results_file)
        if config:
            try:
                await process_api_requests_from_file(config)
                return self._parse_results_file(results_file,client=LLMClient.openai)
            finally:
                if not self.local_cache:
                    self._delete_files(requests_file, results_file)
        return []

    async def _run_anthropic_completion(self, chat_threads: List[ChatThread]) -> List[ProcessedOutput]:
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        requests_file = os.path.join(self.cache_folder, f'anthropic_requests_{timestamp}.jsonl')
        results_file = os.path.join(self.cache_folder, f'anthropic_results_{timestamp}.jsonl')
        self._prepare_requests_file(chat_threads, "anthropic", requests_file)
        config = self._create_anthropic_completion_config(chat_threads[0], requests_file, results_file)
        if config:
            try:
                await process_api_requests_from_file(config)
                return self._parse_results_file(results_file,client=LLMClient.anthropic)
            finally:
                if not self.local_cache:
                    self._delete_files(requests_file, results_file)
        return []
    
    async def _run_vllm_completion(self, chat_threads: List[ChatThread]) -> List[ProcessedOutput]:
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        requests_file = os.path.join(self.cache_folder, f'vllm_requests_{timestamp}.jsonl')
        results_file = os.path.join(self.cache_folder, f'vllm_results_{timestamp}.jsonl')
        self._prepare_requests_file(chat_threads, "vllm", requests_file)
        config = self._create_vllm_completion_config(chat_threads[0], requests_file, results_file)
        if config:
            try:
                await process_api_requests_from_file(config)
                return self._parse_results_file(results_file,client=LLMClient.vllm)
            finally:
                if not self.local_cache:
                    self._delete_files(requests_file, results_file)
        return []
    
    async def _run_litellm_completion(self, chat_threads: List[ChatThread]) -> List[ProcessedOutput]:
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        requests_file = os.path.join(self.cache_folder, f'litellm_requests_{timestamp}.jsonl')
        results_file = os.path.join(self.cache_folder, f'litellm_results_{timestamp}.jsonl')
        self._prepare_requests_file(chat_threads, "litellm", requests_file)
        config = self._create_litellm_completion_config(chat_threads[0], requests_file, results_file)
        if config:
            try:
                await process_api_requests_from_file(config)
                return self._parse_results_file(results_file,client=LLMClient.litellm)
            finally:
                if not self.local_cache:
                    self._delete_files(requests_file, results_file)
        return []

    

    def _prepare_requests_file(self, chat_threads: List[ChatThread], client: str, filename: str):
        requests = []
        for chat_thread in chat_threads:
            request = self._convert_chat_thread_to_request(chat_thread, client)
            if request:
                metadata = {
                    "chat_thread_id": str(chat_thread.id),
                    "start_time": time.time(),
                    "end_time": None,
                    "total_time": None
                }
                requests.append([metadata, request])
        
        with open(filename, 'w') as f:
            for request in requests:
                json.dump(request, f)
                f.write('\n')

    def _validate_anthropic_request(self, request: Dict[str, Any]) -> bool:
        try:
            anthropic_request = AnthropicRequest(**request)
            return True
        except Exception as e:
            raise ValidationError(f"Error validating Anthropic request: {e} with request: {request}")
    
    def _validate_openai_request(self, request: Dict[str, Any]) -> bool:
        try:
            openai_request = OpenAIRequest(**request)
            return True
        except Exception as e:
            print(f"Error validating OpenAI request: {e} with request: {request}")
            raise ValidationError(f"Error validating OpenAI request: {e} with request: {request}")
        
    def _validate_vllm_request(self, request: Dict[str, Any]) -> bool:
        try:
            vllm_request = VLLMRequest(**request)
            return True
        except Exception as e:
            # Instead of raising ValidationError, we'll return False
            raise ValidationError(f"Error validating VLLM request: {e} with request: {request}")
        

    
    def _get_openai_request(self, chat_thread: ChatThread) -> Optional[Dict[str, Any]]:
        messages = chat_thread.oai_messages
        request = {
            "model": chat_thread.llm_config.model,
            "messages": messages,
            "max_tokens": chat_thread.llm_config.max_tokens,
            "temperature": chat_thread.llm_config.temperature,
        }
        if chat_thread.oai_response_format:
            request["response_format"] = chat_thread.oai_response_format
        if chat_thread.llm_config.response_format == "tool" and chat_thread.structured_output:
            tool = chat_thread.structured_output
            if tool:
                request["tools"] = [tool.get_openai_tool()]
                request["tool_choice"] = {"type": "function", "function": {"name": tool.name}}
        elif chat_thread.llm_config.response_format == "auto_tools":
            tools = chat_thread.tools
            if tools:
                request["tools"] = [t.get_openai_tool() for t in tools]
                request["tool_choice"] = "auto"
        if self._validate_openai_request(request):
            return request
        else:
            return None
    
    def _get_anthropic_request(self, chat_thread: ChatThread) -> Optional[Dict[str, Any]]:
        system_content, messages = chat_thread.anthropic_messages    
        request = {
            "model": chat_thread.llm_config.model,
            "max_tokens": chat_thread.llm_config.max_tokens,
            "temperature": chat_thread.llm_config.temperature,
            "messages": messages,
            "system": system_content if system_content else None
        }
        if chat_thread.llm_config.response_format == "tool" and chat_thread.structured_output:
            tool = chat_thread.structured_output
            if tool:
                request["tools"] = [tool.get_anthropic_tool()]
                request["tool_choice"] = ToolChoiceToolChoiceTool(name=tool.name, type="tool")
        elif chat_thread.llm_config.response_format == "auto_tools":
            tools = chat_thread.tools
            if tools:
                request["tools"] = [t.get_anthropic_tool() for t in tools]
                request["tool_choice"] = ToolChoiceToolChoiceAuto(type="auto")

        if self._validate_anthropic_request(request):
            return request
        else:
            return None
        
    def _get_vllm_request(self, chat_thread: ChatThread) -> Optional[Dict[str, Any]]:
        messages = chat_thread.vllm_messages
        request = {
            "model": chat_thread.llm_config.model,
            "messages": messages,
            "max_tokens": chat_thread.llm_config.max_tokens,
            "temperature": chat_thread.llm_config.temperature,
        }
        if chat_thread.llm_config.response_format == "tool" and chat_thread.structured_output:
            tool = chat_thread.structured_output
            if tool:
                request["tools"] = [tool.get_openai_tool()]
                request["tool_choice"] = {"type": "function", "function": {"name": tool.name}}
        if chat_thread.llm_config.response_format == "json_object":
            raise ValueError("VLLM does not support json_object response format otherwise infinite whitespaces are returned")
        if chat_thread.oai_response_format and chat_thread.oai_response_format:
            request["response_format"] = chat_thread.oai_response_format
        
        if self._validate_vllm_request(request):
            return request
        else:
            return None
        
    def _get_litellm_request(self, chat_thread: ChatThread) -> Optional[Dict[str, Any]]:
        if chat_thread.llm_config.response_format == "json_object":
            raise ValueError("VLLM does not support json_object response format otherwise infinite whitespaces are returned")
        return self._get_openai_request(chat_thread)
        
    def _convert_chat_thread_to_request(self, chat_thread: ChatThread, client: str) -> Optional[Dict[str, Any]]:
        if client == "openai":
            return self._get_openai_request(chat_thread)
        elif client == "anthropic":
            return self._get_anthropic_request(chat_thread)
        elif client == "vllm":
            return self._get_vllm_request(chat_thread)
        elif client =="litellm":
            return self._get_litellm_request(chat_thread)
        else:
            raise ValueError(f"Invalid client: {client}")


    def _create_oai_completion_config(self, chat_thread: ChatThread, requests_file: str, results_file: str) -> Optional[OAIApiFromFileConfig]:
        if chat_thread.llm_config.client == "openai" and self.openai_key:
            return OAIApiFromFileConfig(
                requests_filepath=requests_file,
                save_filepath=results_file,
                request_url="https://api.openai.com/v1/chat/completions",
                api_key=self.openai_key,
                max_requests_per_minute=self.oai_request_limits.max_requests_per_minute,
                max_tokens_per_minute=self.oai_request_limits.max_tokens_per_minute,
                token_encoding_name="cl100k_base",
                max_attempts=5,
                logging_level=20,
            )
        return None

    def _create_anthropic_completion_config(self, chat_thread: ChatThread, requests_file: str, results_file: str) -> Optional[OAIApiFromFileConfig]:
        if chat_thread.llm_config.client == "anthropic" and self.anthropic_key:
            return OAIApiFromFileConfig(
                requests_filepath=requests_file,
                save_filepath=results_file,
                request_url="https://api.anthropic.com/v1/messages",
                api_key=self.anthropic_key,
                max_requests_per_minute=self.anthropic_request_limits.max_requests_per_minute,
                max_tokens_per_minute=self.anthropic_request_limits.max_tokens_per_minute,
                token_encoding_name="cl100k_base",
                max_attempts=5,
                logging_level=20,
            )
        return None
    
    def _create_vllm_completion_config(self, chat_thread: ChatThread, requests_file: str, results_file: str) -> Optional[OAIApiFromFileConfig]:
        if chat_thread.llm_config.client == "vllm":
            return OAIApiFromFileConfig(
                requests_filepath=requests_file,
                save_filepath=results_file,
                request_url=self.vllm_endpoint,
                api_key=self.vllm_key if self.vllm_key else "",
                max_requests_per_minute=self.vllm_request_limits.max_requests_per_minute,
                max_tokens_per_minute=self.vllm_request_limits.max_tokens_per_minute,
                token_encoding_name="cl100k_base",
                max_attempts=5,
                logging_level=20,
            )
        return None
    
    def _create_litellm_completion_config(self, chat_thread: ChatThread, requests_file: str, results_file: str) -> Optional[OAIApiFromFileConfig]:
        if chat_thread.llm_config.client == "litellm":
            return OAIApiFromFileConfig(
                requests_filepath=requests_file,
                save_filepath=results_file,
                request_url=self.litellm_endpoint,
                api_key=self.litellm_key if self.litellm_key else "",
                max_requests_per_minute=self.litellm_request_limits.max_requests_per_minute,
                max_tokens_per_minute=self.litellm_request_limits.max_tokens_per_minute,
                token_encoding_name="cl100k_base",
                max_attempts=5,
                logging_level=20,
            )
        return None
    

    def _parse_results_file(self, filepath: str,client: LLMClient) -> List[ProcessedOutput]:
        results = []
        with open(filepath, 'r') as f:
            for line in f:
                try:
                    result = json.loads(line)
                    llm_output = self._convert_result_to_llm_output(result,client)
                    results.append(llm_output)
                except json.JSONDecodeError:
                    print(f"Error decoding JSON: {line}")
                except Exception as e:
                    print(f"Error processing result: {e}")
        return results

    def _convert_result_to_llm_output(self, result: List[Dict[str, Any]],client: LLMClient) -> ProcessedOutput:
        metadata, request_data, response_data = result
        

        raw_output = RawOutput(
            raw_result=response_data,
            completion_kwargs=request_data,
            start_time=metadata["start_time"],
            end_time=metadata["end_time"] or time.time(),
            chat_thread_id=metadata["chat_thread_id"],
            client=client
        )

        return raw_output.create_processed_output()

    def _delete_files(self, *files):
        for file in files:
            try:
                os.remove(file)
            except OSError as e:
                print(f"Error deleting file {file}: {e}")