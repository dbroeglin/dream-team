import asyncio
import logging
import os
from typing import Optional, AsyncGenerator, Dict, Any, List
from datetime import datetime
from dataclasses import asdict
from autogen_core.application import SingleThreadedAgentRuntime
from autogen_core.application.logging import EVENT_LOGGER_NAME
from autogen_core.base import AgentId, AgentProxy
from autogen_core.components import DefaultTopicId
from autogen_ext.code_executors import DockerCommandLineCodeExecutor
from autogen_ext.code_executors import ACADynamicSessionsCodeExecutor
from autogen_core.components.code_executor import CodeBlock
from autogen_magentic_one.agents.coder import Coder, Executor
from autogen_magentic_one.agents.file_surfer import FileSurfer
from autogen_magentic_one.agents.multimodal_web_surfer import MultimodalWebSurfer
from autogen_magentic_one.agents.orchestrator import LedgerOrchestrator
from autogen_magentic_one.messages import BroadcastMessage
from autogen_magentic_one.utils import LogHandler
from autogen_core.components.models import UserMessage
from threading import Lock
from azure.identity import DefaultAzureCredential
import tempfile
from promptflow.tracing import start_trace

#You can view the traces in http://127.0.0.1:23333/v1.0/ui/traces/
start_trace()

## MMA
from autogen_ext.models import AzureOpenAIChatCompletionClient
from dotenv import load_dotenv
load_dotenv()
## end MMA

# Create client
client = AzureOpenAIChatCompletionClient(
    model="gpt-4o",
    api_version="2024-02-01",
    azure_endpoint=os.getenv("AZURE_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    #azure_ad_token_provider=token_provider,
    model_capabilities={
        "vision":True,
        "function_calling":True,
        "json_output":True,
    }
)
pool_endpoint=os.getenv("POOL_MANAGEMENT_ENDPOINT")
azure_credential = DefaultAzureCredential()

async def confirm_code(code: CodeBlock) -> bool:
    return True


class MagenticOneHelper:
    def __init__(self, logs_dir: str = None, save_screenshots: bool = False) -> None:
        """
        A helper class to interact with the MagenticOne system.
        Initialize MagenticOne instance.

        Args:
            logs_dir: Directory to store logs and downloads
            save_screenshots: Whether to save screenshots of web pages
        """
        self.logs_dir = logs_dir or os.getcwd()
        self.runtime: Optional[SingleThreadedAgentRuntime] = None
        self.log_handler: Optional[LogHandler] = None
        self.save_screenshots = save_screenshots

        if not os.path.exists(self.logs_dir):
            os.makedirs(self.logs_dir)

    async def initialize(self) -> None:
        """
        Initialize the MagenticOne system, setting up agents and runtime.
        """
        # Create the runtime
        self.runtime = SingleThreadedAgentRuntime()

        # Set up logging
        logger = logging.getLogger(EVENT_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        self.log_handler = LogHandler(filename=os.path.join(self.logs_dir, "log.jsonl"))
        logger.handlers = [self.log_handler]

        # Set up code executor
        self.code_executor = DockerCommandLineCodeExecutor(work_dir=self.logs_dir)
        await self.code_executor.__aenter__()
        
        '''
        #use this code if you want to use azure container code executor
        with tempfile.TemporaryDirectory() as temp_dir:
            self.code_executor = ACADynamicSessionsCodeExecutor(
                pool_management_endpoint=pool_endpoint, credential=azure_credential, work_dir=temp_dir
            )
        '''

        # Register agents.
        await Coder.register(self.runtime, "Coder", lambda: Coder(model_client=client))
        coder = AgentProxy(AgentId("Coder", "default"), self.runtime)

        await Executor.register(
            self.runtime,
            "Executor",
            lambda: Executor("A agent for executing code", executor=self.code_executor, confirm_execution=confirm_code),
        )
        executor = AgentProxy(AgentId("Executor", "default"), self.runtime)

        await MultimodalWebSurfer.register(self.runtime, "WebSurfer", MultimodalWebSurfer)
        web_surfer = AgentProxy(AgentId("WebSurfer", "default"), self.runtime)

        await FileSurfer.register(self.runtime, "file_surfer", lambda: FileSurfer(model_client=client))
        file_surfer = AgentProxy(AgentId("file_surfer", "default"), self.runtime)

        agent_list = [web_surfer, coder, executor, file_surfer]
        await LedgerOrchestrator.register(
            self.runtime,
            "Orchestrator",
            lambda: LedgerOrchestrator(
                agents=agent_list,
                model_client=client,
                max_rounds=30,
                max_time=25 * 60,
                max_stalls_before_replan=10,
                return_final_answer=True,
            ),
        )

        self.runtime.start()

        actual_surfer = await self.runtime.try_get_underlying_agent_instance(web_surfer.id, type=MultimodalWebSurfer)
        await actual_surfer.init(
            model_client=client,
            downloads_folder=os.getcwd(),
            start_page="https://www.bing.com",
            browser_channel="chromium",
            headless=True,
            debug_dir=self.logs_dir,
            to_save_screenshots=self.save_screenshots,
        )

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """
        Clean up resources.
        """
        if self.code_executor:
            await self.code_executor.__aexit__(exc_type, exc_value, traceback)

    async def run_task(self, task: str) -> None:
        """
        Run a specific task through the MagenticOne system.

        Args:
            task: The task description to be executed
        """
        if not self.runtime:
            raise RuntimeError("MagenticOne not initialized. Call initialize() first.")

        task_message = BroadcastMessage(content=UserMessage(content=task, source="UserProxy"))

        await self.runtime.publish_message(task_message, topic_id=DefaultTopicId())
        await self.runtime.stop_when_idle()

    def get_final_answer(self) -> Optional[str]:
        """
        Get the final answer from the Orchestrator.

        Returns:
            The final answer as a string
        """
        if not self.log_handler:
            raise RuntimeError("Log handler not initialized")

        for log_entry in self.log_handler.logs_list:
            if (
                log_entry.get("type") == "OrchestrationEvent"
                and log_entry.get("source") == "Orchestrator (final answer)"
            ):
                return log_entry.get("message")
        return None

    async def stream_logs(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream logs from the system as they are generated. Stops when it detects both
        the final answer and termination condition from the Orchestrator.

        Yields:
            Dictionary containing log entry information
        """
        if not self.log_handler:
            raise RuntimeError("Log handler not initialized")

        last_index = 0
        found_final_answer = False
        found_termination = False
        found_termination_no_agent = False

        while True:
            current_logs = self.log_handler.logs_list
            while last_index < len(current_logs):
                log_entry = current_logs[last_index]
                yield log_entry
                # Check for termination condition

                if (
                    log_entry.get("type") == "OrchestrationEvent"
                    and log_entry.get("source") == "Orchestrator (final answer)"
                ):
                    found_final_answer = True

                if (
                    log_entry.get("type") == "OrchestrationEvent"
                    and log_entry.get("source") == "Orchestrator (termination condition)"
                ):
                    found_termination = True

                if (
                    log_entry.get("type") == "OrchestrationEvent"
                    and log_entry.get("source") == "Orchestrator (termination condition)"
                    and log_entry.get("message") == "No agent selected."
                ):
                    found_termination_no_agent = True

                if self.runtime._run_context is None:
                    return

                if found_termination_no_agent and found_final_answer:
                    return
                elif found_termination and not found_termination_no_agent:
                    return

                last_index += 1

            await asyncio.sleep(0.1)  # Small delay to prevent busy waiting

    def get_all_logs(self) -> List[Dict[str, Any]]:
        """
        Get all logs that have been collected so far.

        Returns:
            List of all log entries
        """
        if not self.log_handler:
            raise RuntimeError("Log handler not initialized")
        return self.log_handler.logs_list
