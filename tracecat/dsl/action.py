from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from tracecat.contexts import ctx_logger, ctx_run
from tracecat.dsl.common import context_locator
from tracecat.dsl.models import ActionStatement, DSLTaskErrorInfo, RunActionInput
from tracecat.executor.client import ExecutorClient
from tracecat.logger import logger
from tracecat.registry.actions.models import RegistryActionValidateResponse
from tracecat.store.models import StoreObjectPtr
from tracecat.types.auth import Role
from tracecat.types.exceptions import RegistryActionError


def _contextualize_message(
    task: ActionStatement,
    msg: str | BaseException,
    *,
    attempt: int,
    loc: str = "run_action",
) -> str:
    return f"[{context_locator(task, loc)}] (Attempt {attempt})\n\n{msg}"


class ValidateActionActivityInput(BaseModel):
    role: Role
    task: ActionStatement


class DSLActivities:
    """Container for all UDFs registered in the registry."""

    def __new__(cls):  # type: ignore
        raise RuntimeError("This class should not be instantiated")

    @classmethod
    def load(cls) -> list[Callable[[RunActionInput], Any]]:
        """Load and return all UDFs in the class."""
        return [
            getattr(cls, method_name)
            for method_name in dir(cls)
            if hasattr(
                getattr(cls, method_name),
                "__temporal_activity_definition",
            )
        ]

    @staticmethod
    @activity.defn
    async def validate_action_activity(
        input: ValidateActionActivityInput,
    ) -> RegistryActionValidateResponse:
        """Validate an action.
        Goals:
        - Validate the action arguments against the UDF spec.
        - Return the validated arguments.
        """
        client = ExecutorClient(role=input.role)
        return await client.validate_action(
            action_name=input.task.action, args=input.task.args
        )

    @staticmethod
    @activity.defn
    async def run_action_activity(input: RunActionInput, role: Role) -> Any:
        """Run an action.
        Goals:
        - Think of this as a controller activity that will orchestrate the execution of the action.
        - The implementation of the action is located elsewhere (registry service on API)
        """
        ctx_run.set(input.run_context)
        task = input.task
        environment = input.run_context.environment
        action_name = task.action

        act_logger = logger.bind(
            task_ref=task.ref,
            action_name=action_name,
            wf_id=input.run_context.wf_id,
            role=role,
            environment=environment,
        )
        ctx_logger.set(act_logger)

        act_info = activity.info()
        attempt = act_info.attempt
        act_logger.info(
            "Run action activity",
            task=task,
            attempt=attempt,
            retry_policy=task.retry_policy,
        )

        # Add a delay
        if task.start_delay > 0:
            act_logger.info("Starting action with delay", delay=task.start_delay)
            await asyncio.sleep(task.start_delay)

        try:
            # Delegate to the registry client
            client = ExecutorClient(role=role)
            return await client.run_action_memory_backend(input)
        except RegistryActionError as e:
            # We only expect RegistryActionError to be raised from the registry client
            kind = e.__class__.__name__
            msg = str(e)
            err_locator = _contextualize_message(task, msg, attempt=attempt)
            act_logger.error(
                "Application exception occurred", error=msg, detail=e.detail
            )
            err_info = DSLTaskErrorInfo(
                ref=task.ref,
                message=msg,
                type=kind,
                attempt=attempt,
            )
            raise ApplicationError(err_locator, err_info, type=kind) from e
        except ApplicationError as e:
            # Unexpected application error - depends
            act_logger.error("ApplicationError occurred", error=e)
            err_info = DSLTaskErrorInfo(
                ref=task.ref,
                message=str(e),
                type=e.type or e.__class__.__name__,
                attempt=attempt,
            )
            raise ApplicationError(
                _contextualize_message(task, e.message, attempt=attempt),
                err_info,
                non_retryable=e.non_retryable,
                type=e.type,
            ) from e
        except Exception as e:
            # Unexpected errors - non-retryable
            kind = e.__class__.__name__
            raw_msg = f"{kind} occurred:\n{e}"
            act_logger.error(raw_msg)

            err_info = DSLTaskErrorInfo(
                ref=task.ref,
                message=raw_msg,
                type=kind,
                attempt=attempt,
            )
            raise ApplicationError(
                _contextualize_message(task, raw_msg, attempt=attempt),
                err_info,
                type=kind,
                non_retryable=True,
            ) from e

    @staticmethod
    @activity.defn
    async def run_action_with_store_activity(
        input: RunActionInput, role: Role
    ) -> StoreObjectPtr:
        """
        Run an action in store mode.

        Note
        ----
        We must only pass store object pointers across activities/workflows.
        Do not pass the objects themselves as they are too heavy to be transferred over the boundary.
        DSLContext should just keep track of the references to the objects, not the objects themselves.
        """
        ctx_run.set(input.run_context)
        log = logger.bind(
            task_ref=input.task.ref,
            action_name=input.task.action,
            wf_id=input.run_context.wf_id,
            role=role,
            environment=input.run_context.environment,
        )
        ctx_logger.set(log)

        attempt = activity.info().attempt
        log.info(
            "Run action activity",
            task=input.task,
            attempt=attempt,
            retry_policy=input.task.retry_policy,
        )

        # Add a delay
        if input.task.start_delay > 0:
            log.info("Starting action with delay", delay=input.task.start_delay)
            await asyncio.sleep(input.task.start_delay)

        try:
            # Delegate to the registry client
            client = ExecutorClient(role=role)
            handle = await client.run_action_store_backend(input)
            return handle.to_pointer()
        except Exception as e:
            # Now that we return ActionRefHandle, these are transient errors
            kind = e.__class__.__name__
            raise ApplicationError(
                _contextualize_message(input.task, e, attempt=attempt),
                DSLTaskErrorInfo(
                    ref=input.task.ref, message=str(e), type=kind, attempt=attempt
                ),
            ) from e
