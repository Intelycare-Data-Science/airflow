#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import contextlib
import hashlib
import itertools
import logging
import math
import operator
import os
import signal
import traceback
from collections import defaultdict
from collections.abc import Collection, Generator, Iterable, Mapping, Sequence
from datetime import timedelta
from enum import Enum
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

import attrs
import dill
import jinja2
import lazy_object_proxy
import pendulum
import uuid6
from jinja2 import TemplateAssertionError, UndefinedError
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    and_,
    case,
    delete,
    extract,
    false,
    func,
    inspect,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import lazyload, reconstructor, relationship
from sqlalchemy.orm.attributes import NO_VALUE, set_committed_value
from sqlalchemy_utils import UUIDType

from airflow import settings
from airflow.assets.manager import asset_manager
from airflow.configuration import conf
from airflow.exceptions import (
    AirflowException,
    AirflowFailException,
    AirflowInactiveAssetInInletOrOutletException,
    AirflowRescheduleException,
    AirflowSensorTimeout,
    AirflowSkipException,
    AirflowTaskTerminated,
    AirflowTaskTimeout,
    TaskDeferralError,
    TaskDeferred,
    UnmappableXComLengthPushed,
    UnmappableXComTypePushed,
    XComForMappingNotPushed,
)
from airflow.listeners.listener import get_listener_manager
from airflow.models.asset import AssetActive, AssetEvent, AssetModel
from airflow.models.base import Base, StringID, TaskInstanceDependencies
from airflow.models.dagbag import DagBag
from airflow.models.log import Log
from airflow.models.renderedtifields import get_serialized_template_fields
from airflow.models.taskinstancekey import TaskInstanceKey
from airflow.models.taskmap import TaskMap
from airflow.models.taskreschedule import TaskReschedule
from airflow.models.xcom import LazyXComSelectSequence, XComModel
from airflow.plugins_manager import integrate_macros_plugins
from airflow.sdk.execution_time.context import context_to_airflow_vars
from airflow.sentry import Sentry
from airflow.settings import task_instance_mutation_hook
from airflow.stats import Stats
from airflow.ti_deps.dep_context import DepContext
from airflow.ti_deps.dependencies_deps import REQUEUEABLE_DEPS, RUNNING_DEPS
from airflow.utils import timezone
from airflow.utils.email import send_email
from airflow.utils.helpers import prune_dict, render_template_to_string
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.utils.net import get_hostname
from airflow.utils.platform import getuser
from airflow.utils.retries import run_with_db_retries
from airflow.utils.session import NEW_SESSION, create_session, provide_session
from airflow.utils.span_status import SpanStatus
from airflow.utils.sqlalchemy import ExecutorConfigType, ExtendedJSON, UtcDateTime
from airflow.utils.state import DagRunState, State, TaskInstanceState
from airflow.utils.task_instance_session import set_current_task_instance_session
from airflow.utils.timeout import timeout
from airflow.utils.xcom import XCOM_RETURN_KEY

TR = TaskReschedule

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import PurePath
    from types import TracebackType

    from sqlalchemy.engine import Connection as SAConnection, Engine
    from sqlalchemy.orm.session import Session
    from sqlalchemy.sql import Update
    from sqlalchemy.sql.elements import BooleanClauseList
    from sqlalchemy.sql.expression import ColumnOperators

    from airflow.models.abstractoperator import TaskStateChangeCallback
    from airflow.models.baseoperator import BaseOperator
    from airflow.models.dag import DAG as SchedulerDAG, DagModel
    from airflow.models.dagrun import DagRun
    from airflow.sdk.api.datamodels._generated import AssetProfile
    from airflow.sdk.definitions._internal.abstractoperator import Operator
    from airflow.sdk.definitions.asset import AssetNameRef, AssetUniqueKey, AssetUriRef
    from airflow.sdk.definitions.dag import DAG
    from airflow.sdk.definitions.taskgroup import MappedTaskGroup
    from airflow.sdk.types import RuntimeTaskInstanceProtocol
    from airflow.typing_compat import Literal
    from airflow.utils.context import Context
    from airflow.utils.task_group import TaskGroup


PAST_DEPENDS_MET = "past_depends_met"


class TaskReturnCode(Enum):
    """
    Enum to signal manner of exit for task run command.

    :meta private:
    """

    DEFERRED = 100
    """When task exits with deferral to trigger."""


@provide_session
def _add_log(
    event,
    task_instance=None,
    owner=None,
    owner_display_name=None,
    extra=None,
    session: Session = NEW_SESSION,
    **kwargs,
):
    session.add(
        Log(
            event,
            task_instance,
            owner,
            owner_display_name,
            extra,
            **kwargs,
        )
    )


@provide_session
def _update_ti_heartbeat(id: str, when: datetime, session: Session = NEW_SESSION):
    session.execute(update(TaskInstance).where(TaskInstance.id == id).values(last_heartbeat_at=when))


def _run_raw_task(
    ti: TaskInstance,
    mark_success: bool = False,
    test_mode: bool = False,
    pool: str | None = None,
    raise_on_defer: bool = False,
    session: Session | None = None,
) -> TaskReturnCode | None:
    """
    Run a task, update the state upon completion, and run any appropriate callbacks.

    Immediately runs the task (without checking or changing db state
    before execution) and then sets the appropriate final state after
    completion and runs any post-execute callbacks. Meant to be called
    only after another function changes the state to running.

    :param mark_success: Don't run the task, mark its state as success
    :param test_mode: Doesn't record success or failure in the DB
    :param pool: specifies the pool to use to run the task instance
    :param session: SQLAlchemy ORM Session
    """
    if TYPE_CHECKING:
        assert isinstance(ti.task, BaseOperator)

    ti.test_mode = test_mode
    ti.refresh_from_task(ti.task, pool_override=pool)
    ti.refresh_from_db(session=session)
    ti.hostname = get_hostname()
    ti.pid = os.getpid()
    if not test_mode:
        TaskInstance.save_to_db(ti=ti, session=session)
    actual_start_date = timezone.utcnow()
    Stats.incr(f"ti.start.{ti.task.dag_id}.{ti.task.task_id}", tags=ti.stats_tags)
    # Same metric with tagging
    Stats.incr("ti.start", tags=ti.stats_tags)
    # Initialize final state counters at zero
    for state in State.task_states:
        Stats.incr(
            f"ti.finish.{ti.task.dag_id}.{ti.task.task_id}.{state}",
            count=0,
            tags=ti.stats_tags,
        )
        # Same metric with tagging
        Stats.incr(
            "ti.finish",
            count=0,
            tags={**ti.stats_tags, "state": str(state)},
        )
    with set_current_task_instance_session(session=session):
        ti.task = ti.task.prepare_for_execution()
        context = ti.get_template_context(ignore_param_exceptions=False, session=session)

        try:
            if ti.task:
                from airflow.sdk.definitions.asset import Asset

                inlets = [asset.asprofile() for asset in ti.task.inlets if isinstance(asset, Asset)]
                outlets = [asset.asprofile() for asset in ti.task.outlets if isinstance(asset, Asset)]
                TaskInstance.validate_inlet_outlet_assets_activeness(inlets, outlets, session=session)
            if not mark_success:
                TaskInstance._execute_task_with_callbacks(
                    self=ti,  # type: ignore[arg-type]
                    context=context,
                    test_mode=test_mode,
                    session=session,
                )
            if not test_mode:
                ti.refresh_from_db(lock_for_update=True, session=session, keep_local_changes=True)
            ti.state = TaskInstanceState.SUCCESS
        except TaskDeferred as defer:
            # The task has signalled it wants to defer execution based on
            # a trigger.
            if raise_on_defer:
                raise
            ti.defer_task(exception=defer, session=session)
            ti.log.info(
                "Pausing task as DEFERRED. dag_id=%s, task_id=%s, run_id=%s, logical_date=%s, start_date=%s",
                ti.dag_id,
                ti.task_id,
                ti.run_id,
                _date_or_empty(task_instance=ti, attr="logical_date"),
                _date_or_empty(task_instance=ti, attr="start_date"),
            )
            return TaskReturnCode.DEFERRED
        except AirflowSkipException as e:
            # Recording SKIP
            # log only if exception has any arguments to prevent log flooding
            if e.args:
                ti.log.info(e)
            if not test_mode:
                ti.refresh_from_db(lock_for_update=True, session=session, keep_local_changes=True)
            ti.state = TaskInstanceState.SKIPPED
            _run_finished_callback(callbacks=ti.task.on_skipped_callback, context=context)
            TaskInstance.save_to_db(ti=ti, session=session)
        except AirflowRescheduleException as reschedule_exception:
            ti._handle_reschedule(actual_start_date, reschedule_exception, test_mode, session=session)
            ti.log.info("Rescheduling task, marking task as UP_FOR_RESCHEDULE")
            return None
        except (AirflowFailException, AirflowSensorTimeout) as e:
            # If AirflowFailException is raised, task should not retry.
            # If a sensor in reschedule mode reaches timeout, task should not retry.
            ti.handle_failure(e, test_mode, context, force_fail=True, session=session)  # already saves to db
            raise
        except (AirflowTaskTimeout, AirflowException, AirflowTaskTerminated) as e:
            if not test_mode:
                ti.refresh_from_db(lock_for_update=True, session=session)
            # for case when task is marked as success/failed externally
            # or dagrun timed out and task is marked as skipped
            # current behavior doesn't hit the callbacks
            if ti.state in State.finished:
                ti.clear_next_method_args()
                TaskInstance.save_to_db(ti=ti, session=session)
                return None
            else:
                ti.handle_failure(e, test_mode, context, session=session)
                raise
        except SystemExit as e:
            # We have already handled SystemExit with success codes (0 and None) in the `_execute_task`.
            # Therefore, here we must handle only error codes.
            msg = f"Task failed due to SystemExit({e.code})"
            ti.handle_failure(msg, test_mode, context, session=session)
            raise AirflowException(msg)
        except BaseException as e:
            ti.handle_failure(e, test_mode, context, session=session)
            raise
        finally:
            # Print a marker post execution for internals of post task processing
            log.info("::group::Post task execution logs")

            Stats.incr(
                f"ti.finish.{ti.dag_id}.{ti.task_id}.{ti.state}",
                tags=ti.stats_tags,
            )
            # Same metric with tagging
            Stats.incr("ti.finish", tags={**ti.stats_tags, "state": str(ti.state)})

        # Recording SKIPPED or SUCCESS
        ti.clear_next_method_args()
        ti.end_date = timezone.utcnow()
        _log_state(task_instance=ti)
        ti.set_duration()

        # run on_success_callback before db committing
        # otherwise, the LocalTaskJob sees the state is changed to `success`,
        # but the task_runner is still running, LocalTaskJob then treats the state is set externally!
        if ti.state == TaskInstanceState.SUCCESS:
            _run_finished_callback(callbacks=ti.task.on_success_callback, context=context)

        if not test_mode:
            _add_log(event=ti.state, task_instance=ti, session=session)
            if ti.state == TaskInstanceState.SUCCESS:
                from airflow.sdk.execution_time.task_runner import (
                    _build_asset_profiles,
                    _serialize_outlet_events,
                )

                TaskInstance.register_asset_changes_in_db(
                    ti,
                    list(_build_asset_profiles(ti.task.outlets)),
                    list(_serialize_outlet_events(context["outlet_events"])),
                    session=session,
                )

            TaskInstance.save_to_db(ti=ti, session=session)
            if ti.state == TaskInstanceState.SUCCESS:
                try:
                    get_listener_manager().hook.on_task_instance_success(
                        previous_state=TaskInstanceState.RUNNING, task_instance=ti
                    )
                except Exception:
                    log.exception("error calling listener")
        return None


@contextlib.contextmanager
def set_current_context(context: Context) -> Generator[Context, None, None]:
    """
    Set the current execution context to the provided context object.

    This method should be called once per Task execution, before calling operator.execute.
    """
    from airflow.sdk.definitions._internal.contextmanager import _CURRENT_CONTEXT

    _CURRENT_CONTEXT.append(context)
    try:
        yield context
    finally:
        expected_state = _CURRENT_CONTEXT.pop()
        if expected_state != context:
            log.warning(
                "Current context is not equal to the state at context stack. Expected=%s, got=%s",
                context,
                expected_state,
            )


def _stop_remaining_tasks(*, task_instance: TaskInstance, session: Session):
    """
    Stop non-teardown tasks in dag.

    :meta private:
    """
    if not task_instance.dag_run:
        raise ValueError("``task_instance`` must have ``dag_run`` set")
    tis = task_instance.dag_run.get_task_instances(session=session)
    if TYPE_CHECKING:
        assert task_instance.task
        assert isinstance(task_instance.task.dag, DAG)

    for ti in tis:
        if ti.task_id == task_instance.task_id or ti.state in (
            TaskInstanceState.SUCCESS,
            TaskInstanceState.FAILED,
        ):
            continue
        task = task_instance.task.dag.task_dict[ti.task_id]
        if not task.is_teardown:
            if ti.state == TaskInstanceState.RUNNING:
                log.info("Forcing task %s to fail due to dag's `fail_fast` setting", ti.task_id)
                ti.error(session)
            else:
                log.info("Setting task %s to SKIPPED due to dag's `fail_fast` setting.", ti.task_id)
                ti.set_state(state=TaskInstanceState.SKIPPED, session=session)
        else:
            log.info("Not skipping teardown task '%s'", ti.task_id)


def clear_task_instances(
    tis: list[TaskInstance],
    session: Session,
    dag: DAG | None = None,
    dag_run_state: DagRunState | Literal[False] = DagRunState.QUEUED,
) -> None:
    """
    Clear a set of task instances, but make sure the running ones get killed.

    Also sets Dagrun's `state` to QUEUED and `start_date` to the time of execution.
    But only for finished DRs (SUCCESS and FAILED).
    Doesn't clear DR's `state` and `start_date`for running
    DRs (QUEUED and RUNNING) because clearing the state for already
    running DR is redundant and clearing `start_date` affects DR's duration.

    :param tis: a list of task instances
    :param session: current session
    :param dag_run_state: state to set finished DagRuns to.
        If set to False, DagRuns state will not be changed.
    :param dag: DAG object
    """
    # taskinstance uuids:
    task_instance_ids: list[str] = []
    dag_bag = DagBag(read_dags_from_db=True)
    from airflow.models.taskinstancehistory import TaskInstanceHistory

    for ti in tis:
        task_instance_ids.append(ti.id)
        TaskInstanceHistory.record_ti(ti, session)
        ti.try_id = uuid7()
        if ti.state == TaskInstanceState.RUNNING:
            # If a task is cleared when running, set its state to RESTARTING so that
            # the task is terminated and becomes eligible for retry.
            ti.state = TaskInstanceState.RESTARTING
        else:
            ti_dag = dag if dag and dag.dag_id == ti.dag_id else dag_bag.get_dag(ti.dag_id, session=session)
            task_id = ti.task_id
            if ti_dag and ti_dag.has_task(task_id):
                task = ti_dag.get_task(task_id)
                ti.refresh_from_task(task)
                if TYPE_CHECKING:
                    assert ti.task
                ti.max_tries = ti.try_number + task.retries
            else:
                # Ignore errors when updating max_tries if the DAG or
                # task are not found since database records could be
                # outdated. We make max_tries the maximum value of its
                # original max_tries or the last attempted try number.
                ti.max_tries = max(ti.max_tries, ti.try_number)
            ti.state = None
            ti.external_executor_id = None
            ti.clear_next_method_args()
            session.merge(ti)

    if task_instance_ids:
        # Clear all reschedules related to the ti to clear
        delete_qry = TR.__table__.delete().where(TR.ti_id.in_(task_instance_ids))
        session.execute(delete_qry)

    if dag_run_state is not False and tis:
        from airflow.models.dagrun import DagRun  # Avoid circular import

        run_ids_by_dag_id = defaultdict(set)
        for instance in tis:
            run_ids_by_dag_id[instance.dag_id].add(instance.run_id)

        drs = (
            session.query(DagRun)
            .filter(
                or_(
                    and_(DagRun.dag_id == dag_id, DagRun.run_id.in_(run_ids))
                    for dag_id, run_ids in run_ids_by_dag_id.items()
                )
            )
            .all()
        )
        dag_run_state = DagRunState(dag_run_state)  # Validate the state value.
        for dr in drs:
            if dr.state in State.finished_dr_states:
                dr.state = dag_run_state
                dr.start_date = timezone.utcnow()
                if dag_run_state == DagRunState.QUEUED:
                    dr.last_scheduling_decision = None
                    dr.start_date = None
                    dr.clear_number += 1
    session.flush()


@provide_session
def _xcom_pull(
    *,
    ti,
    task_ids: str | Iterable[str] | None = None,
    dag_id: str | None = None,
    key: str = XCOM_RETURN_KEY,
    include_prior_dates: bool = False,
    session: Session = NEW_SESSION,
    map_indexes: int | Iterable[int] | None = None,
    default: Any = None,
    run_id: str | None = None,
) -> Any:
    if dag_id is None:
        dag_id = ti.dag_id
    if run_id is None:
        run_id = ti.run_id

    query = XComModel.get_many(
        key=key,
        run_id=run_id,
        dag_ids=dag_id,
        task_ids=task_ids,
        map_indexes=map_indexes,
        include_prior_dates=include_prior_dates,
        session=session,
    )

    # NOTE: Since we're only fetching the value field and not the whole
    # class, the @recreate annotation does not kick in. Therefore we need to
    # call XCom.deserialize_value() manually.

    # We are only pulling one single task.
    if (task_ids is None or isinstance(task_ids, str)) and not isinstance(map_indexes, Iterable):
        first = query.with_entities(
            XComModel.run_id, XComModel.task_id, XComModel.dag_id, XComModel.map_index, XComModel.value
        ).first()
        if first is None:  # No matching XCom at all.
            return default
        if map_indexes is not None or first.map_index < 0:
            return XComModel.deserialize_value(first)

        # raise RuntimeError("Nothing should hit this anymore")

    # TODO: TaskSDK: We should remove this, but many tests still currently call `ti.run()`. See #45549

    # At this point either task_ids or map_indexes is explicitly multi-value.
    # Order return values to match task_ids and map_indexes ordering.
    ordering = []
    if task_ids is None or isinstance(task_ids, str):
        ordering.append(XComModel.task_id)
    elif task_id_whens := {tid: i for i, tid in enumerate(task_ids)}:
        ordering.append(case(task_id_whens, value=XComModel.task_id))
    else:
        ordering.append(XComModel.task_id)
    if map_indexes is None or isinstance(map_indexes, int):
        ordering.append(XComModel.map_index)
    elif isinstance(map_indexes, range):
        order = XComModel.map_index
        if map_indexes.step < 0:
            order = order.desc()
        ordering.append(order)
    elif map_index_whens := {map_index: i for i, map_index in enumerate(map_indexes)}:
        ordering.append(case(map_index_whens, value=XComModel.map_index))
    else:
        ordering.append(XComModel.map_index)
    return LazyXComSelectSequence.from_select(
        query.with_entities(XComModel.value).order_by(None).statement,
        order_by=ordering,
        session=session,
    )


def _creator_note(val):
    """Creator the ``note`` association proxy."""
    if isinstance(val, str):
        return TaskInstanceNote(content=val)
    elif isinstance(val, dict):
        return TaskInstanceNote(**val)
    else:
        return TaskInstanceNote(*val)


def _execute_task(task_instance: TaskInstance, context: Context, task_orig: Operator):
    """
    Execute Task (optionally with a Timeout) and push Xcom results.

    :param task_instance: the task instance
    :param context: Jinja2 context
    :param task_orig: origin task

    :meta private:
    """
    from airflow.sdk.bases.operator import ExecutorSafeguard
    from airflow.sdk.definitions.mappedoperator import MappedOperator

    task_to_execute = task_instance.task

    if TYPE_CHECKING:
        # TODO: TaskSDK this function will need 100% re-writing
        # This only works with a "rich" BaseOperator, not the SDK version
        assert isinstance(task_to_execute, BaseOperator)

    if isinstance(task_to_execute, MappedOperator):
        raise AirflowException("MappedOperator cannot be executed.")

    # If the task has been deferred and is being executed due to a trigger,
    # then we need to pick the right method to come back to, otherwise
    # we go for the default execute
    execute_callable_kwargs: dict[str, Any] = {}
    execute_callable: Callable
    if task_instance.next_method:
        execute_callable = task_to_execute.resume_execution
        execute_callable_kwargs["next_method"] = task_instance.next_method
        # We don't want modifictions we make here to be tracked by SQLA
        execute_callable_kwargs["next_kwargs"] = {**(task_instance.next_kwargs or {})}
        if task_instance.next_method == "execute":
            execute_callable_kwargs["next_kwargs"][f"{task_to_execute.__class__.__name__}__sentinel"] = (
                ExecutorSafeguard.sentinel_value
            )
    else:
        execute_callable = task_to_execute.execute
        if execute_callable.__name__ == "execute":
            execute_callable_kwargs[f"{task_to_execute.__class__.__name__}__sentinel"] = (
                ExecutorSafeguard.sentinel_value
            )

    def _execute_callable(context: Context, **execute_callable_kwargs):
        from airflow.sdk.execution_time.callback_runner import create_executable_runner
        from airflow.sdk.execution_time.context import context_get_outlet_events

        try:
            # Print a marker for log grouping of details before task execution
            log.info("::endgroup::")

            return create_executable_runner(
                execute_callable,
                context_get_outlet_events(context),
                logger=log,
            ).run(context=context, **execute_callable_kwargs)
        except SystemExit as e:
            # Handle only successful cases here. Failure cases will be handled upper
            # in the exception chain.
            if e.code is not None and e.code != 0:
                raise
            return None

    # If a timeout is specified for the task, make it fail
    # if it goes beyond
    if task_to_execute.execution_timeout:
        # If we are coming in with a next_method (i.e. from a deferral),
        # calculate the timeout from our start_date.
        if task_instance.next_method and task_instance.start_date:
            timeout_seconds = (
                task_to_execute.execution_timeout - (timezone.utcnow() - task_instance.start_date)
            ).total_seconds()
        else:
            timeout_seconds = task_to_execute.execution_timeout.total_seconds()
        try:
            # It's possible we're already timed out, so fast-fail if true
            if timeout_seconds <= 0:
                raise AirflowTaskTimeout()
            # Run task in timeout wrapper
            with timeout(timeout_seconds):
                result = _execute_callable(context=context, **execute_callable_kwargs)
        except AirflowTaskTimeout:
            task_to_execute.on_kill()
            raise
    else:
        result = _execute_callable(context=context, **execute_callable_kwargs)
    cm = create_session()
    with cm as session_or_null:
        if task_to_execute.do_xcom_push:
            xcom_value = result
        else:
            xcom_value = None
        if xcom_value is not None:  # If the task returns a result, push an XCom containing it.
            if task_to_execute.multiple_outputs:
                if not isinstance(xcom_value, Mapping):
                    raise AirflowException(
                        f"Returned output was type {type(xcom_value)} "
                        "expected dictionary for multiple_outputs"
                    )
                for key in xcom_value.keys():
                    if not isinstance(key, str):
                        raise AirflowException(
                            "Returned dictionary keys must be strings when using "
                            f"multiple_outputs, found {key} ({type(key)}) instead"
                        )
                for key, value in xcom_value.items():
                    task_instance.xcom_push(key=key, value=value, session=session_or_null)
            task_instance.xcom_push(key=XCOM_RETURN_KEY, value=xcom_value, session=session_or_null)
        if TYPE_CHECKING:
            assert task_orig.dag
        _record_task_map_for_downstreams(
            task_instance=task_instance,
            task=task_orig,
            dag=task_orig.dag,
            value=xcom_value,
            session=session_or_null,
        )
    return result


def _set_duration(*, task_instance: TaskInstance) -> None:
    """
    Set task instance duration.

    :param task_instance: the task instance

    :meta private:
    """
    if task_instance.end_date and task_instance.start_date:
        task_instance.duration = (task_instance.end_date - task_instance.start_date).total_seconds()
    else:
        task_instance.duration = None
    log.debug("Task Duration set to %s", task_instance.duration)


def _stats_tags(*, task_instance: TaskInstance) -> dict[str, str]:
    """
    Return task instance tags.

    :param task_instance: the task instance

    :meta private:
    """
    return prune_dict({"dag_id": task_instance.dag_id, "task_id": task_instance.task_id})


def _clear_next_method_args(*, task_instance: TaskInstance) -> None:
    """
    Ensure we unset next_method and next_kwargs to ensure that any retries don't reuse them.

    :param task_instance: the task instance

    :meta private:
    """
    log.debug("Clearing next_method and next_kwargs.")

    task_instance.next_method = None
    task_instance.next_kwargs = None


def _get_template_context(
    *,
    task_instance: TaskInstance,
    dag: SchedulerDAG,
    session: Session | None = None,
    ignore_param_exceptions: bool = True,
) -> Context:
    """
    Return TI Context.

    :param task_instance: the task instance for the task
    :param dag: dag for the task
    :param session: SQLAlchemy ORM Session
    :param ignore_param_exceptions: flag to suppress value exceptions while initializing the ParamsDict

    :meta private:
    """
    # Do not use provide_session here -- it expunges everything on exit!
    if not session:
        session = settings.Session()

    from airflow import macros
    from airflow.models.abstractoperator import NotMapped
    from airflow.models.baseoperator import BaseOperator
    from airflow.sdk.api.datamodels._generated import (
        DagRun as DagRunSDK,
        PrevSuccessfulDagRunResponse,
        TIRunContext,
    )
    from airflow.sdk.definitions.param import process_params
    from airflow.sdk.execution_time.context import InletEventsAccessors
    from airflow.utils.context import (
        ConnectionAccessor,
        OutletEventAccessors,
        VariableAccessor,
    )

    integrate_macros_plugins()

    task = task_instance.task
    if TYPE_CHECKING:
        assert task_instance.task
        assert task
        assert task.dag
        assert session

    def _get_dagrun(session: Session) -> DagRun:
        dag_run = task_instance.get_dagrun(session)
        if dag_run in session:
            return dag_run
        # The dag_run may not be attached to the session anymore since the
        # code base is over-zealous with use of session.expunge_all().
        # Re-attach it if the relation is not loaded so we can load it when needed.
        info = inspect(dag_run)
        if info.attrs.consumed_asset_events.loaded_value is not NO_VALUE:
            return dag_run
        # If dag_run is not flushed to db at all (e.g. CLI commands using
        # in-memory objects for ad-hoc operations), just set the value manually.
        if not info.has_identity:
            dag_run.consumed_asset_events = []
            return dag_run
        return session.merge(dag_run, load=False)

    dag_run = _get_dagrun(session)

    validated_params = process_params(dag, task, dag_run.conf, suppress_exception=ignore_param_exceptions)
    ti_context_from_server = TIRunContext(
        dag_run=DagRunSDK.model_validate(dag_run, from_attributes=True),
        max_tries=task_instance.max_tries,
        should_retry=task_instance.is_eligible_to_retry(),
    )
    runtime_ti = task_instance.to_runtime_ti(context_from_server=ti_context_from_server)

    context: Context = runtime_ti.get_template_context()

    @cache  # Prevent multiple database access.
    def _get_previous_dagrun_success() -> PrevSuccessfulDagRunResponse:
        dr_from_db = task_instance.get_previous_dagrun(state=DagRunState.SUCCESS, session=session)
        if dr_from_db:
            return PrevSuccessfulDagRunResponse.model_validate(dr_from_db, from_attributes=True)
        return PrevSuccessfulDagRunResponse()

    def get_prev_data_interval_start_success() -> pendulum.DateTime | None:
        return timezone.coerce_datetime(_get_previous_dagrun_success().data_interval_start)

    def get_prev_data_interval_end_success() -> pendulum.DateTime | None:
        return timezone.coerce_datetime(_get_previous_dagrun_success().data_interval_end)

    def get_prev_start_date_success() -> pendulum.DateTime | None:
        return timezone.coerce_datetime(_get_previous_dagrun_success().start_date)

    def get_prev_end_date_success() -> pendulum.DateTime | None:
        return timezone.coerce_datetime(_get_previous_dagrun_success().end_date)

    def get_triggering_events() -> dict[str, list[AssetEvent]]:
        asset_events = dag_run.consumed_asset_events
        triggering_events: dict[str, list[AssetEvent]] = defaultdict(list)
        for event in asset_events:
            if event.asset:
                triggering_events[event.asset.uri].append(event)

        return triggering_events

    # NOTE: If you add to this dict, make sure to also update the following:
    # * Context in task-sdk/src/airflow/sdk/definitions/context.py
    # * KNOWN_CONTEXT_KEYS in airflow/utils/context.py
    # * Table in docs/apache-airflow/templates-ref.rst

    context.update(
        {
            "outlet_events": OutletEventAccessors(),
            "inlet_events": InletEventsAccessors(task.inlets),
            "macros": macros,
            "params": validated_params,
            "prev_data_interval_start_success": get_prev_data_interval_start_success(),
            "prev_data_interval_end_success": get_prev_data_interval_end_success(),
            "prev_start_date_success": get_prev_start_date_success(),
            "prev_end_date_success": get_prev_end_date_success(),
            "test_mode": task_instance.test_mode,
            # ti/task_instance are added here for ti.xcom_{push,pull}
            "task_instance": task_instance,
            "ti": task_instance,
            "triggering_asset_events": lazy_object_proxy.Proxy(get_triggering_events),
            "var": {
                "json": VariableAccessor(deserialize_json=True),
                "value": VariableAccessor(deserialize_json=False),
            },
            "conn": ConnectionAccessor(),
        }
    )

    try:
        expanded_ti_count: int | None = BaseOperator.get_mapped_ti_count(
            task, task_instance.run_id, session=session
        )
        context["expanded_ti_count"] = expanded_ti_count
        if expanded_ti_count:
            setattr(
                task_instance,
                "_upstream_map_indexes",
                {
                    upstream.task_id: task_instance.get_relevant_upstream_map_indexes(
                        upstream,
                        expanded_ti_count,
                        session=session,
                    )
                    for upstream in task.upstream_list
                },
            )
    except NotMapped:
        pass

    return context


def _is_eligible_to_retry(*, task_instance: TaskInstance):
    """
    Is task instance is eligible for retry.

    :param task_instance: the task instance

    :meta private:
    """
    if task_instance.state == TaskInstanceState.RESTARTING:
        # If a task is cleared when running, it goes into RESTARTING state and is always
        # eligible for retry
        return True
    if not getattr(task_instance, "task", None):
        # Couldn't load the task, don't know number of retries, guess:
        return task_instance.try_number <= task_instance.max_tries

    if TYPE_CHECKING:
        assert task_instance.task

    return task_instance.task.retries and task_instance.try_number <= task_instance.max_tries


@provide_session
def _handle_failure(
    *,
    task_instance: TaskInstance,
    error: None | str | BaseException,
    session: Session,
    test_mode: bool | None = None,
    context: Context | None = None,
    force_fail: bool = False,
    fail_fast: bool = False,
) -> None:
    """
    Handle Failure for a task instance.

    :param task_instance: the task instance
    :param error: if specified, log the specific exception if thrown
    :param session: SQLAlchemy ORM Session
    :param test_mode: doesn't record success or failure in the DB if True
    :param context: Jinja2 context
    :param force_fail: if True, task does not retry

    :meta private:
    """
    if test_mode is None:
        test_mode = task_instance.test_mode
    failure_context = TaskInstance.fetch_handle_failure_context(
        ti=task_instance,  # type: ignore[arg-type]
        error=error,
        test_mode=test_mode,
        context=context,
        force_fail=force_fail,
        session=session,
        fail_fast=fail_fast,
    )

    _log_state(task_instance=task_instance, lead_msg="Immediate failure requested. " if force_fail else "")
    if (
        failure_context["task"]
        and failure_context["email_for_state"](failure_context["task"])
        and failure_context["task"].email
    ):
        try:
            task_instance.email_alert(error, failure_context["task"])
        except Exception:
            log.exception("Failed to send email to: %s", failure_context["task"].email)

    if failure_context["callbacks"] and failure_context["context"]:
        _run_finished_callback(
            callbacks=failure_context["callbacks"],
            context=failure_context["context"],
        )

    if not test_mode:
        TaskInstance.save_to_db(failure_context["ti"], session)


def _refresh_from_task(
    *, task_instance: TaskInstance, task: Operator, pool_override: str | None = None
) -> None:
    """
    Copy common attributes from the given task.

    :param task_instance: the task instance
    :param task: The task object to copy from
    :param pool_override: Use the pool_override instead of task's pool

    :meta private:
    """
    task_instance.task = task
    task_instance.queue = task.queue
    task_instance.pool = pool_override or task.pool
    task_instance.pool_slots = task.pool_slots
    with contextlib.suppress(Exception):
        # This method is called from the different places, and sometimes the TI is not fully initialized
        task_instance.priority_weight = task_instance.task.weight_rule.get_weight(
            task_instance  # type: ignore[arg-type]
        )
    task_instance.run_as_user = task.run_as_user
    # Do not set max_tries to task.retries here because max_tries is a cumulative
    # value that needs to be stored in the db.
    task_instance.executor = task.executor
    task_instance.executor_config = task.executor_config
    task_instance.operator = task.task_type
    task_instance.custom_operator_name = getattr(task, "custom_operator_name", None)
    # Re-apply cluster policy here so that task default do not overload previous data
    task_instance_mutation_hook(task_instance)


@provide_session
def _record_task_map_for_downstreams(
    *,
    task_instance: TaskInstance,
    task: Operator,
    dag: DAG,
    value: Any,
    session: Session,
) -> None:
    """
    Record the task map for downstream tasks.

    :param task_instance: the task instance
    :param task: The task object
    :param dag: the dag associated with the task
    :param value: The value
    :param session: SQLAlchemy ORM Session

    :meta private:
    """
    from airflow.sdk.definitions.mappedoperator import MappedOperator, is_mappable_value

    if next(task.iter_mapped_dependants(), None) is None:  # No mapped dependants, no need to validate.
        return
    # TODO: We don't push TaskMap for mapped task instances because it's not
    #  currently possible for a downstream to depend on one individual mapped
    #  task instance. This will change when we implement task mapping inside
    #  a mapped task group, and we'll need to further analyze the case.
    if isinstance(task, MappedOperator):
        return
    if value is None:
        raise XComForMappingNotPushed()
    if not is_mappable_value(value):
        raise UnmappableXComTypePushed(value)
    task_map = TaskMap.from_task_instance_xcom(task_instance, value)
    max_map_length = conf.getint("core", "max_map_length", fallback=1024)
    if task_map.length > max_map_length:
        raise UnmappableXComLengthPushed(value, max_map_length)
    session.merge(task_map)


def _get_previous_dagrun(
    *,
    task_instance: TaskInstance,
    state: DagRunState | None = None,
    session: Session | None = None,
) -> DagRun | None:
    """
    Return the DagRun that ran prior to this task instance's DagRun.

    :param task_instance: the task instance
    :param state: If passed, it only take into account instances of a specific state.
    :param session: SQLAlchemy ORM Session.

    :meta private:
    """
    if TYPE_CHECKING:
        assert task_instance.task

    dag = task_instance.task.dag
    if dag is None:
        return None

    if TYPE_CHECKING:
        assert isinstance(dag, SchedulerDAG)
    dr = task_instance.get_dagrun(session=session)
    dr.dag = dag

    from airflow.models.dagrun import DagRun  # Avoid circular import

    # We always ignore schedule in dagrun lookup when `state` is given
    # or the DAG is never scheduled. For legacy reasons, when
    # `catchup=True`, we use `get_previous_scheduled_dagrun` unless
    # `ignore_schedule` is `True`.
    ignore_schedule = state is not None or not dag.timetable.can_be_scheduled
    if dag.catchup is True and not ignore_schedule:
        last_dagrun = DagRun.get_previous_scheduled_dagrun(dr.id, session=session)
    else:
        last_dagrun = DagRun.get_previous_dagrun(dag_run=dr, session=session, state=state)

    if last_dagrun:
        return last_dagrun

    return None


def _get_previous_logical_date(
    *,
    task_instance: TaskInstance,
    state: DagRunState | None,
    session: Session,
) -> pendulum.DateTime | None:
    """
    Get logical date from property previous_ti_success.

    :param task_instance: the task instance
    :param session: SQLAlchemy ORM Session
    :param state: If passed, it only take into account instances of a specific state.

    :meta private:
    """
    log.debug("previous_logical_date was called")
    prev_ti = task_instance.get_previous_ti(state=state, session=session)
    return pendulum.instance(prev_ti.logical_date) if prev_ti and prev_ti.logical_date else None


def _get_previous_start_date(
    *,
    task_instance: TaskInstance,
    state: DagRunState | None,
    session: Session,
) -> pendulum.DateTime | None:
    """
    Return the start date from property previous_ti_success.

    :param task_instance: the task instance
    :param state: If passed, it only take into account instances of a specific state.
    :param session: SQLAlchemy ORM Session
    """
    log.debug("previous_start_date was called")
    prev_ti = task_instance.get_previous_ti(state=state, session=session)
    # prev_ti may not exist and prev_ti.start_date may be None.
    return pendulum.instance(prev_ti.start_date) if prev_ti and prev_ti.start_date else None


def _email_alert(*, task_instance: TaskInstance, exception, task: BaseOperator) -> None:
    """
    Send alert email with exception information.

    :param task_instance: the task instance
    :param exception: the exception
    :param task: task related to the exception

    :meta private:
    """
    subject, html_content, html_content_err = task_instance.get_email_subject_content(exception, task=task)
    if TYPE_CHECKING:
        assert task.email
    try:
        send_email(task.email, subject, html_content)
    except Exception:
        send_email(task.email, subject, html_content_err)


def _get_email_subject_content(
    *,
    task_instance: TaskInstance | RuntimeTaskInstanceProtocol,
    exception: BaseException,
    task: BaseOperator | None = None,
) -> tuple[str, str, str]:
    """
    Get the email subject content for exceptions.

    :param task_instance: the task instance
    :param exception: the exception sent in the email
    :param task:

    :meta private:
    """
    # For a ti from DB (without ti.task), return the default value
    if task is None:
        task = getattr(task_instance, "task")
    use_default = task is None
    exception_html = str(exception).replace("\n", "<br>")

    default_subject = "Airflow alert: {{ti}}"
    # For reporting purposes, we report based on 1-indexed,
    # not 0-indexed lists (i.e. Try 1 instead of
    # Try 0 for the first attempt).
    default_html_content = (
        "Try {{try_number}} out of {{max_tries + 1}}<br>"
        "Exception:<br>{{exception_html}}<br>"
        'Log: <a href="{{ti.log_url}}">Link</a><br>'
        "Host: {{ti.hostname}}<br>"
        'Mark success: <a href="{{ti.mark_success_url}}">Link</a><br>'
    )

    default_html_content_err = (
        "Try {{try_number}} out of {{max_tries + 1}}<br>"
        "Exception:<br>Failed attempt to attach error logs<br>"
        'Log: <a href="{{ti.log_url}}">Link</a><br>'
        "Host: {{ti.hostname}}<br>"
        'Mark success: <a href="{{ti.mark_success_url}}">Link</a><br>'
    )

    additional_context: dict[str, Any] = {
        "exception": exception,
        "exception_html": exception_html,
        "try_number": task_instance.try_number,
        "max_tries": task_instance.max_tries,
    }

    if use_default:
        default_context = {"ti": task_instance, **additional_context}
        jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(os.path.dirname(__file__)), autoescape=True
        )
        subject = jinja_env.from_string(default_subject).render(**default_context)
        html_content = jinja_env.from_string(default_html_content).render(**default_context)
        html_content_err = jinja_env.from_string(default_html_content_err).render(**default_context)

    else:
        from airflow.sdk.definitions._internal.templater import SandboxedEnvironment
        from airflow.utils.context import context_merge

        if TYPE_CHECKING:
            assert task_instance.task

        # Use the DAG's get_template_env() to set force_sandboxed. Don't add
        # the flag to the function on task object -- that function can be
        # overridden, and adding a flag breaks backward compatibility.
        dag = task_instance.task.get_dag()
        if dag:
            jinja_env = dag.get_template_env(force_sandboxed=True)
        else:
            jinja_env = SandboxedEnvironment(cache_size=0)
        jinja_context = task_instance.get_template_context()
        context_merge(jinja_context, additional_context)

        def render(key: str, content: str) -> str:
            if conf.has_option("email", key):
                path = conf.get_mandatory_value("email", key)
                try:
                    with open(path) as f:
                        content = f.read()
                except FileNotFoundError:
                    log.warning("Could not find email template file '%s'. Using defaults...", path)
                except OSError:
                    log.exception("Error while using email template %s. Using defaults...", path)
            return render_template_to_string(jinja_env.from_string(content), jinja_context)

        subject = render("subject_template", default_subject)
        html_content = render("html_content_template", default_html_content)
        html_content_err = render("html_content_template", default_html_content_err)

    return subject, html_content, html_content_err


def _run_finished_callback(
    *,
    callbacks: None | TaskStateChangeCallback | Sequence[TaskStateChangeCallback],
    context: Context,
) -> None:
    """
    Run callback after task finishes.

    :param callbacks: callbacks to run
    :param context: callbacks context

    :meta private:
    """
    if callbacks:
        callbacks = callbacks if isinstance(callbacks, Sequence) else [callbacks]

        def get_callback_representation(callback: TaskStateChangeCallback) -> Any:
            with contextlib.suppress(AttributeError):
                return callback.__name__
            with contextlib.suppress(AttributeError):
                return callback.__class__.__name__
            return callback

        for idx, callback in enumerate(callbacks):
            callback_repr = get_callback_representation(callback)
            log.info("Executing callback at index %d: %s", idx, callback_repr)
            try:
                callback(context)
            except Exception:
                log.exception("Error in callback at index %d: %s", idx, callback_repr)


def _log_state(*, task_instance: TaskInstance, lead_msg: str = "") -> None:
    """
    Log task state.

    :param task_instance: the task instance
    :param lead_msg: lead message

    :meta private:
    """
    params = [
        lead_msg,
        str(task_instance.state).upper(),
        task_instance.dag_id,
        task_instance.task_id,
        task_instance.run_id,
    ]
    message = "%sMarking task as %s. dag_id=%s, task_id=%s, run_id=%s, "
    if task_instance.map_index >= 0:
        params.append(task_instance.map_index)
        message += "map_index=%d, "
    message += "logical_date=%s, start_date=%s, end_date=%s"
    log.info(
        message,
        *params,
        _date_or_empty(task_instance=task_instance, attr="logical_date"),
        _date_or_empty(task_instance=task_instance, attr="start_date"),
        _date_or_empty(task_instance=task_instance, attr="end_date"),
        stacklevel=2,
    )


def _date_or_empty(*, task_instance: TaskInstance, attr: str) -> str:
    """
    Fetch a date attribute or None of it does not exist.

    :param task_instance: the task instance
    :param attr: the attribute name

    :meta private:
    """
    result: datetime | None = getattr(task_instance, attr, None)
    return result.strftime("%Y%m%dT%H%M%S") if result else ""


def _get_previous_ti(
    *,
    task_instance: TaskInstance,
    session: Session,
    state: DagRunState | None = None,
) -> TaskInstance | None:
    """
    Get task instance for the task that ran before this task instance.

    :param task_instance: the task instance
    :param state: If passed, it only take into account instances of a specific state.
    :param session: SQLAlchemy ORM Session

    :meta private:
    """
    dagrun = task_instance.get_previous_dagrun(state, session=session)
    if dagrun is None:
        return None
    return dagrun.get_task_instance(task_instance.task_id, session=session)


@provide_session
def _update_rtif(ti, rendered_fields, session: Session = NEW_SESSION):
    from airflow.models.renderedtifields import RenderedTaskInstanceFields

    rtif = RenderedTaskInstanceFields(ti=ti, render_templates=False, rendered_fields=rendered_fields)
    RenderedTaskInstanceFields.write(rtif, session=session)
    session.flush()
    RenderedTaskInstanceFields.delete_old_records(ti.task_id, ti.dag_id, session=session)


@provide_session
def _defer_task(
    ti: TaskInstance,
    exception: TaskDeferred | None = None,
    session: Session = NEW_SESSION,
) -> TaskInstance:
    from airflow.models.trigger import Trigger

    # TODO: TaskSDK add start_trigger_args to SDK definitions
    if TYPE_CHECKING:
        assert ti.task is None or isinstance(ti.task, BaseOperator)

    timeout: timedelta | None
    if exception is not None:
        trigger_row = Trigger.from_object(exception.trigger)
        next_method = exception.method_name
        next_kwargs = exception.kwargs
        timeout = exception.timeout
    elif ti.task is not None and ti.task.start_trigger_args is not None:
        context = ti.get_template_context()
        start_trigger_args = ti.task.expand_start_trigger_args(context=context, session=session)
        if start_trigger_args is None:
            raise TaskDeferralError(
                "A none 'None' start_trigger_args has been change to 'None' during expandion"
            )

        trigger_kwargs = start_trigger_args.trigger_kwargs or {}
        next_kwargs = start_trigger_args.next_kwargs
        next_method = start_trigger_args.next_method
        timeout = start_trigger_args.timeout
        trigger_row = Trigger(
            classpath=ti.task.start_trigger_args.trigger_cls,
            kwargs=trigger_kwargs,
        )
    else:
        raise TaskDeferralError("exception and ti.task.start_trigger_args cannot both be None")

    # First, make the trigger entry
    session.add(trigger_row)
    session.flush()

    if TYPE_CHECKING:
        assert ti.task

    # Then, update ourselves so it matches the deferral request
    # Keep an eye on the logic in `check_and_change_state_before_execution()`
    # depending on self.next_method semantics
    ti.state = TaskInstanceState.DEFERRED
    ti.trigger_id = trigger_row.id
    ti.next_method = next_method
    ti.next_kwargs = next_kwargs or {}

    # Calculate timeout too if it was passed
    if timeout is not None:
        ti.trigger_timeout = timezone.utcnow() + timeout
    else:
        ti.trigger_timeout = None

    # If an execution_timeout is set, set the timeout to the minimum of
    # it and the trigger timeout
    execution_timeout = ti.task.execution_timeout
    if execution_timeout:
        if TYPE_CHECKING:
            assert ti.start_date
        if ti.trigger_timeout:
            ti.trigger_timeout = min(ti.start_date + execution_timeout, ti.trigger_timeout)
        else:
            ti.trigger_timeout = ti.start_date + execution_timeout
    if ti.test_mode:
        _add_log(event=ti.state, task_instance=ti, session=session)

    if exception is not None:
        session.merge(ti)
        session.commit()
    return ti


@provide_session
def _handle_reschedule(
    ti,
    actual_start_date: datetime,
    reschedule_exception: AirflowRescheduleException,
    test_mode: bool = False,
    session: Session = NEW_SESSION,
):
    # Don't record reschedule request in test mode
    if test_mode:
        return

    ti.refresh_from_db(session)

    if TYPE_CHECKING:
        assert ti.task

    ti.end_date = timezone.utcnow()
    ti.set_duration()

    # set state
    ti.state = TaskInstanceState.UP_FOR_RESCHEDULE

    ti.clear_next_method_args()

    session.merge(ti)
    session.commit()

    # we add this in separate commit to reduce likelihood of deadlock
    # see https://github.com/apache/airflow/pull/21362 for more info
    session.add(
        TaskReschedule(
            ti.id,
            ti.try_number,
            actual_start_date,
            ti.end_date,
            reschedule_exception.reschedule_date,
        )
    )
    session.commit()
    return ti


def uuid7() -> str:
    """Generate a new UUID7 string."""
    return str(uuid6.uuid7())


class TaskInstance(Base, LoggingMixin):
    """
    Task instances store the state of a task instance.

    This table is the authority and single source of truth around what tasks
    have run and the state they are in.

    The SqlAlchemy model doesn't have a SqlAlchemy foreign key to the task or
    dag model deliberately to have more control over transactions.

    Database transactions on this table should insure double triggers and
    any confusion around what task instances are or aren't ready to run
    even while multiple schedulers may be firing task instances.

    A value of -1 in map_index represents any of: a TI without mapped tasks;
    a TI with mapped tasks that has yet to be expanded (state=pending);
    a TI with mapped tasks that expanded to an empty list (state=skipped).
    """

    __tablename__ = "task_instance"
    id = Column(
        String(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql"),
        primary_key=True,
        default=uuid7,
        nullable=False,
    )
    task_id = Column(StringID(), nullable=False)
    dag_id = Column(StringID(), nullable=False)
    run_id = Column(StringID(), nullable=False)
    map_index = Column(Integer, nullable=False, server_default=text("-1"))

    start_date = Column(UtcDateTime)
    end_date = Column(UtcDateTime)
    duration = Column(Float)
    state = Column(String(20))
    try_id = Column(UUIDType(binary=False), default=uuid7, unique=True, nullable=False)
    try_number = Column(Integer, default=0)
    max_tries = Column(Integer, server_default=text("-1"))
    hostname = Column(String(1000))
    unixname = Column(String(1000))
    pool = Column(String(256), nullable=False)
    pool_slots = Column(Integer, default=1, nullable=False)
    queue = Column(String(256))
    priority_weight = Column(Integer)
    operator = Column(String(1000))
    custom_operator_name = Column(String(1000))
    queued_dttm = Column(UtcDateTime)
    scheduled_dttm = Column(UtcDateTime)
    queued_by_job_id = Column(Integer)

    last_heartbeat_at = Column(UtcDateTime)
    pid = Column(Integer)
    executor = Column(String(1000))
    executor_config = Column(ExecutorConfigType(pickler=dill))
    updated_at = Column(UtcDateTime, default=timezone.utcnow, onupdate=timezone.utcnow)
    _rendered_map_index = Column("rendered_map_index", String(250))
    context_carrier = Column(MutableDict.as_mutable(ExtendedJSON))
    span_status = Column(String(250), server_default=SpanStatus.NOT_STARTED, nullable=False)

    external_executor_id = Column(StringID())

    # The trigger to resume on if we are in state DEFERRED
    trigger_id = Column(Integer)

    # Optional timeout utcdatetime for the trigger (past this, we'll fail)
    trigger_timeout = Column(UtcDateTime)

    # The method to call next, and any extra arguments to pass to it.
    # Usually used when resuming from DEFERRED.
    next_method = Column(String(1000))
    next_kwargs = Column(MutableDict.as_mutable(ExtendedJSON))

    _task_display_property_value = Column("task_display_name", String(2000), nullable=True)
    dag_version_id = Column(UUIDType(binary=False), ForeignKey("dag_version.id", ondelete="CASCADE"))
    dag_version = relationship("DagVersion", back_populates="task_instances")

    __table_args__ = (
        Index("ti_dag_state", dag_id, state),
        Index("ti_dag_run", dag_id, run_id),
        Index("ti_state", state),
        Index("ti_state_lkp", dag_id, task_id, run_id, state),
        Index("ti_pool", pool, state, priority_weight),
        Index("ti_trigger_id", trigger_id),
        Index("ti_heartbeat", last_heartbeat_at),
        PrimaryKeyConstraint("id", name="task_instance_pkey"),
        UniqueConstraint("dag_id", "task_id", "run_id", "map_index", name="task_instance_composite_key"),
        ForeignKeyConstraint(
            [trigger_id],
            ["trigger.id"],
            name="task_instance_trigger_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [dag_id, run_id],
            ["dag_run.dag_id", "dag_run.run_id"],
            name="task_instance_dag_run_fkey",
            ondelete="CASCADE",
        ),
    )

    dag_model: DagModel = relationship(
        "DagModel",
        primaryjoin="TaskInstance.dag_id == DagModel.dag_id",
        foreign_keys=dag_id,
        uselist=False,
        innerjoin=True,
        viewonly=True,
    )

    trigger = relationship("Trigger", uselist=False, back_populates="task_instance")
    triggerer_job = association_proxy("trigger", "triggerer_job")
    dag_run = relationship("DagRun", back_populates="task_instances", lazy="joined", innerjoin=True)
    rendered_task_instance_fields = relationship("RenderedTaskInstanceFields", lazy="noload", uselist=False)
    run_after = association_proxy("dag_run", "run_after")
    logical_date = association_proxy("dag_run", "logical_date")
    task_instance_note = relationship(
        "TaskInstanceNote",
        back_populates="task_instance",
        uselist=False,
        cascade="all, delete, delete-orphan",
    )
    note = association_proxy("task_instance_note", "content", creator=_creator_note)

    task: Operator | None = None
    test_mode: bool = False
    is_trigger_log_context: bool = False
    run_as_user: str | None = None
    raw: bool | None = None
    """Indicate to FileTaskHandler that logging context should be set up for trigger logging.

    :meta private:
    """
    _logger_name = "airflow.task"

    def __init__(
        self,
        task: Operator,
        run_id: str | None = None,
        state: str | None = None,
        map_index: int = -1,
        dag_version_id: UUIDType | None = None,
    ):
        super().__init__()
        self.dag_id = task.dag_id
        self.task_id = task.task_id
        self.map_index = map_index
        self.dag_version_id = dag_version_id
        self.refresh_from_task(task)
        if TYPE_CHECKING:
            assert self.task

        # init_on_load will config the log
        self.init_on_load()

        self.run_id = run_id
        self.try_number = 0
        self.max_tries = self.task.retries
        if not self.id:
            self.id = uuid7()
        self.unixname = getuser()
        if state:
            self.state = state
        self.hostname = ""
        # Is this TaskInstance being currently running within `airflow tasks run --raw`.
        # Not persisted to the database so only valid for the current process
        self.raw = False
        # can be changed when calling 'run'
        self.test_mode = False
        self.context_carrier = {}

    def __hash__(self):
        return hash((self.task_id, self.dag_id, self.run_id, self.map_index))

    @property
    def stats_tags(self) -> dict[str, str]:
        """Returns task instance tags."""
        return _stats_tags(task_instance=self)

    @staticmethod
    def insert_mapping(
        run_id: str, task: Operator, map_index: int, dag_version_id: UUIDType | None
    ) -> dict[str, Any]:
        """
        Insert mapping.

        :meta private:
        """
        priority_weight = task.weight_rule.get_weight(
            TaskInstance(task=task, run_id=run_id, map_index=map_index)
        )

        return {
            "dag_id": task.dag_id,
            "task_id": task.task_id,
            "run_id": run_id,
            "try_number": 0,
            "hostname": "",
            "unixname": getuser(),
            "queue": task.queue,
            "pool": task.pool,
            "pool_slots": task.pool_slots,
            "priority_weight": priority_weight,
            "run_as_user": task.run_as_user,
            "max_tries": task.retries,
            "executor": task.executor,
            "executor_config": task.executor_config,
            "operator": task.task_type,
            "custom_operator_name": getattr(task, "custom_operator_name", None),
            "map_index": map_index,
            "_task_display_property_value": task.task_display_name,
            "dag_version_id": dag_version_id,
        }

    @reconstructor
    def init_on_load(self) -> None:
        """Initialize the attributes that aren't stored in the DB."""
        self.test_mode = False  # can be changed when calling 'run'

    @property
    def operator_name(self) -> str | None:
        """@property: use a more friendly display name for the operator, if set."""
        return self.custom_operator_name or self.operator

    @hybrid_property
    def task_display_name(self) -> str:
        return self._task_display_property_value or self.task_id

    @hybrid_property
    def rendered_map_index(self) -> str | None:
        if self._rendered_map_index is not None:
            return self._rendered_map_index
        if self.map_index >= 0:
            return str(self.map_index)
        return None

    @classmethod
    def from_runtime_ti(cls, runtime_ti: RuntimeTaskInstanceProtocol) -> TaskInstance:
        if runtime_ti.map_index is None:
            runtime_ti.map_index = -1
        ti = TaskInstance(
            run_id=runtime_ti.run_id,
            task=runtime_ti.task,  # type: ignore[arg-type]
            map_index=runtime_ti.map_index,
        )

        if TYPE_CHECKING:
            assert ti
            assert isinstance(ti, TaskInstance)
        return ti

    def to_runtime_ti(self, context_from_server) -> RuntimeTaskInstanceProtocol:
        from airflow.sdk.execution_time.task_runner import RuntimeTaskInstance

        runtime_ti = RuntimeTaskInstance.model_construct(
            id=self.id,
            task_id=self.task_id,
            dag_id=self.dag_id,
            run_id=self.run_id,
            try_numer=self.try_number,
            map_index=self.map_index,
            task=self.task,
            max_tries=self.max_tries,
            hostname=self.hostname,
            _ti_context_from_server=context_from_server,
            start_date=self.start_date,
        )

        return runtime_ti

    @staticmethod
    def _command_as_list(
        ti: TaskInstance,
        mark_success: bool = False,
        ignore_all_deps: bool = False,
        ignore_task_deps: bool = False,
        ignore_depends_on_past: bool = False,
        wait_for_past_depends_before_skipping: bool = False,
        ignore_ti_state: bool = False,
        local: bool = False,
        raw: bool = False,
        pool: str | None = None,
        cfg_path: str | None = None,
    ) -> list[str]:
        dag: DAG | DagModel | None
        # Use the dag if we have it, else fallback to the ORM dag_model, which might not be loaded
        if hasattr(ti, "task") and getattr(ti.task, "dag", None) is not None:
            if TYPE_CHECKING:
                assert ti.task
                assert isinstance(ti.task.dag, SchedulerDAG)
            dag = ti.task.dag
        else:
            dag = ti.dag_model

        if dag is None:
            raise ValueError("DagModel is empty")

        path = None
        if dag.relative_fileloc:
            path = Path(dag.relative_fileloc)

        if path:
            if not path.is_absolute():
                path = "DAGS_FOLDER" / path

        return TaskInstance.generate_command(
            ti.dag_id,
            ti.task_id,
            run_id=ti.run_id,
            mark_success=mark_success,
            ignore_all_deps=ignore_all_deps,
            ignore_task_deps=ignore_task_deps,
            ignore_depends_on_past=ignore_depends_on_past,
            wait_for_past_depends_before_skipping=wait_for_past_depends_before_skipping,
            ignore_ti_state=ignore_ti_state,
            local=local,
            file_path=path,
            raw=raw,
            pool=pool,
            cfg_path=cfg_path,
            map_index=ti.map_index,
        )

    def command_as_list(
        self,
        mark_success: bool = False,
        ignore_all_deps: bool = False,
        ignore_task_deps: bool = False,
        ignore_depends_on_past: bool = False,
        wait_for_past_depends_before_skipping: bool = False,
        ignore_ti_state: bool = False,
        local: bool = False,
        raw: bool = False,
        pool: str | None = None,
        cfg_path: str | None = None,
    ) -> list[str]:
        """
        Return a command that can be executed anywhere where airflow is installed.

        This command is part of the message sent to executors by the orchestrator.
        """
        return TaskInstance._command_as_list(
            ti=self,
            mark_success=mark_success,
            ignore_all_deps=ignore_all_deps,
            ignore_task_deps=ignore_task_deps,
            ignore_depends_on_past=ignore_depends_on_past,
            wait_for_past_depends_before_skipping=wait_for_past_depends_before_skipping,
            ignore_ti_state=ignore_ti_state,
            local=local,
            raw=raw,
            pool=pool,
            cfg_path=cfg_path,
        )

    @staticmethod
    def generate_command(
        dag_id: str,
        task_id: str,
        run_id: str,
        mark_success: bool = False,
        ignore_all_deps: bool = False,
        ignore_depends_on_past: bool = False,
        wait_for_past_depends_before_skipping: bool = False,
        ignore_task_deps: bool = False,
        ignore_ti_state: bool = False,
        local: bool = False,
        file_path: PurePath | str | None = None,
        raw: bool = False,
        pool: str | None = None,
        cfg_path: str | None = None,
        map_index: int = -1,
    ) -> list[str]:
        """
        Generate the shell command required to execute this task instance.

        :param dag_id: DAG ID
        :param task_id: Task ID
        :param run_id: The run_id of this task's DagRun
        :param mark_success: Whether to mark the task as successful
        :param ignore_all_deps: Ignore all ignorable dependencies.
            Overrides the other ignore_* parameters.
        :param ignore_depends_on_past: Ignore depends_on_past parameter of DAGs
            (e.g. for Backfills)
        :param wait_for_past_depends_before_skipping: Wait for past depends before marking the ti as skipped
        :param ignore_task_deps: Ignore task-specific dependencies such as depends_on_past
            and trigger rule
        :param ignore_ti_state: Ignore the task instance's previous failure/success
        :param local: Whether to run the task locally
        :param file_path: path to the file containing the DAG definition
        :param raw: raw mode (needs more details)
        :param pool: the Airflow pool that the task should run in
        :param cfg_path: the Path to the configuration file
        :return: shell command that can be used to run the task instance
        """
        cmd = ["airflow", "tasks", "run", dag_id, task_id, run_id]
        if mark_success:
            cmd.extend(["--mark-success"])
        if ignore_all_deps:
            cmd.extend(["--ignore-all-dependencies"])
        if ignore_task_deps:
            cmd.extend(["--ignore-dependencies"])
        if ignore_depends_on_past:
            cmd.extend(["--depends-on-past", "ignore"])
        elif wait_for_past_depends_before_skipping:
            cmd.extend(["--depends-on-past", "wait"])
        if ignore_ti_state:
            cmd.extend(["--force"])
        if local:
            cmd.extend(["--local"])
        if pool:
            cmd.extend(["--pool", pool])
        if raw:
            cmd.extend(["--raw"])
        if file_path:
            cmd.extend(["--subdir", os.fspath(file_path)])
        if cfg_path:
            cmd.extend(["--cfg-path", cfg_path])
        if map_index != -1:
            cmd.extend(["--map-index", str(map_index)])
        return cmd

    @property
    def log_url(self) -> str:
        """Log URL for TaskInstance."""
        run_id = quote(self.run_id)
        base_url = conf.get_mandatory_value("webserver", "BASE_URL")
        map_index = f"&map_index={self.map_index}" if self.map_index >= 0 else ""
        _log_uri = (
            f"{base_url}"
            f"/dags"
            f"/{self.dag_id}"
            f"/grid"
            f"?dag_run_id={run_id}"
            f"&task_id={self.task_id}"
            f"{map_index}"
            "&tab=logs"
        )
        if self.logical_date:
            base_date = quote(self.logical_date.strftime("%Y-%m-%dT%H:%M:%S%z"))
            _log_uri = f"{_log_uri}&base_date={base_date}"
        return _log_uri

    @property
    def mark_success_url(self) -> str:
        """URL to mark TI success."""
        base_url = conf.get_mandatory_value("webserver", "BASE_URL")
        return (
            f"{base_url}"
            "/confirm"
            f"?task_id={self.task_id}"
            f"&dag_id={self.dag_id}"
            f"&dag_run_id={quote(self.run_id)}"
            "&upstream=false"
            "&downstream=false"
            "&state=success"
        )

    @provide_session
    def current_state(self, session: Session = NEW_SESSION) -> str:
        """
        Get the very latest state from the database.

        If a session is passed, we use and looking up the state becomes part of the session,
        otherwise a new session is used.

        sqlalchemy.inspect is used here to get the primary keys ensuring that if they change
        it will not regress

        :param session: SQLAlchemy ORM Session
        """
        filters = (col == getattr(self, col.name) for col in inspect(TaskInstance).primary_key)
        return session.query(TaskInstance.state).filter(*filters).scalar()

    @provide_session
    def error(self, session: Session = NEW_SESSION) -> None:
        """
        Force the task instance's state to FAILED in the database.

        :param session: SQLAlchemy ORM Session
        """
        self.log.error("Recording the task instance as FAILED")
        self.state = TaskInstanceState.FAILED
        session.merge(self)
        session.commit()

    @classmethod
    @provide_session
    def get_task_instance(
        cls,
        dag_id: str,
        run_id: str,
        task_id: str,
        map_index: int,
        lock_for_update: bool = False,
        session: Session = NEW_SESSION,
    ) -> TaskInstance | None:
        query = (
            session.query(TaskInstance)
            .options(lazyload(TaskInstance.dag_run))  # lazy load dag run to avoid locking it
            .filter_by(
                dag_id=dag_id,
                run_id=run_id,
                task_id=task_id,
                map_index=map_index,
            )
        )

        if lock_for_update:
            for attempt in run_with_db_retries(logger=cls.logger()):
                with attempt:
                    return query.with_for_update().one_or_none()
        else:
            return query.one_or_none()

        return None

    @provide_session
    def refresh_from_db(
        self, session: Session = NEW_SESSION, lock_for_update: bool = False, keep_local_changes: bool = False
    ) -> None:
        """
        Refresh the task instance from the database based on the primary key.

        :param session: SQLAlchemy ORM Session
        :param lock_for_update: if True, indicates that the database should
            lock the TaskInstance (issuing a FOR UPDATE clause) until the
            session is committed.
        :param keep_local_changes: Force all attributes to the values from the database if False (the default),
            or if True don't overwrite locally set attributes
        """
        source = TaskInstance.get_task_instance(
            dag_id=self.dag_id,
            task_id=self.task_id,
            run_id=self.run_id,
            map_index=self.map_index,
            lock_for_update=lock_for_update,
            session=session,
        )
        if source:
            from sqlalchemy.orm import attributes

            source_state = inspect(source)
            if source_state is None:
                raise RuntimeError(f"Unable to inspect SQLAlchemy state of {type(source)}: {source}")
            target_state = inspect(self)
            if target_state is None:
                raise RuntimeError(f"Unable to inspect SQLAlchemy state of {type(self)}: {self}")
            for name, attr in source_state.attrs.items():
                if keep_local_changes and target_state.attrs[name].history.has_changes():
                    continue

                val = attr.loaded_value

                if val is not attributes.NO_VALUE:
                    set_committed_value(self, name, val)

            target_state.key = source_state.key
        else:
            self.state = None

    def refresh_from_task(self, task: Operator, pool_override: str | None = None) -> None:
        """
        Copy common attributes from the given task.

        :param task: The task object to copy from
        :param pool_override: Use the pool_override instead of task's pool
        """
        _refresh_from_task(task_instance=self, task=task, pool_override=pool_override)

    @staticmethod
    @provide_session
    def _clear_xcom_data(ti: TaskInstance, session: Session = NEW_SESSION) -> None:
        """
        Clear all XCom data from the database for the task instance.

        If the task is unmapped, all XComs matching this task ID in the same DAG
        run are removed. If the task is mapped, only the one with matching map
        index is removed.

        :param ti: The TI for which we need to clear xcoms.
        :param session: SQLAlchemy ORM Session
        """
        ti.log.debug("Clearing XCom data")
        if ti.map_index < 0:
            map_index: int | None = None
        else:
            map_index = ti.map_index
        XComModel.clear(
            dag_id=ti.dag_id,
            task_id=ti.task_id,
            run_id=ti.run_id,
            map_index=map_index,
            session=session,
        )

    @provide_session
    def clear_xcom_data(self, session: Session = NEW_SESSION):
        self._clear_xcom_data(ti=self, session=session)

    @property
    def key(self) -> TaskInstanceKey:
        """Returns a tuple that identifies the task instance uniquely."""
        return TaskInstanceKey(self.dag_id, self.task_id, self.run_id, self.try_number, self.map_index)

    @staticmethod
    def _set_state(ti: TaskInstance, state, session: Session) -> bool:
        if not isinstance(ti, TaskInstance):
            ti = session.scalars(
                select(TaskInstance).where(
                    TaskInstance.task_id == ti.task_id,
                    TaskInstance.dag_id == ti.dag_id,
                    TaskInstance.run_id == ti.run_id,
                    TaskInstance.map_index == ti.map_index,
                )
            ).one()

        if ti.state == state:
            return False

        current_time = timezone.utcnow()
        ti.log.debug("Setting task state for %s to %s", ti, state)
        ti.state = state
        ti.start_date = ti.start_date or current_time
        if ti.state in State.finished or ti.state == TaskInstanceState.UP_FOR_RETRY:
            ti.end_date = ti.end_date or current_time
            ti.duration = (ti.end_date - ti.start_date).total_seconds()

        session.merge(ti)
        return True

    @provide_session
    def set_state(self, state: str | None, session: Session = NEW_SESSION) -> bool:
        """
        Set TaskInstance state.

        :param state: State to set for the TI
        :param session: SQLAlchemy ORM Session
        :return: Was the state changed
        """
        return self._set_state(ti=self, state=state, session=session)

    @property
    def is_premature(self) -> bool:
        """Returns whether a task is in UP_FOR_RETRY state and its retry interval has elapsed."""
        # is the task still in the retry waiting period?
        return self.state == TaskInstanceState.UP_FOR_RETRY and not self.ready_for_retry()

    @provide_session
    def are_dependents_done(self, session: Session = NEW_SESSION) -> bool:
        """
        Check whether the immediate dependents of this task instance have succeeded or have been skipped.

        This is meant to be used by wait_for_downstream.

        This is useful when you do not want to start processing the next
        schedule of a task until the dependents are done. For instance,
        if the task DROPs and recreates a table.

        :param session: SQLAlchemy ORM Session
        """
        task = self.task
        if TYPE_CHECKING:
            assert task

        if not task.downstream_task_ids:
            return True

        ti = session.query(func.count(TaskInstance.task_id)).filter(
            TaskInstance.dag_id == self.dag_id,
            TaskInstance.task_id.in_(task.downstream_task_ids),
            TaskInstance.run_id == self.run_id,
            TaskInstance.state.in_((TaskInstanceState.SKIPPED, TaskInstanceState.SUCCESS)),
        )
        count = ti[0][0]
        return count == len(task.downstream_task_ids)

    @provide_session
    def get_previous_dagrun(
        self,
        state: DagRunState | None = None,
        session: Session | None = None,
    ) -> DagRun | None:
        """
        Return the DagRun that ran before this task instance's DagRun.

        :param state: If passed, it only take into account instances of a specific state.
        :param session: SQLAlchemy ORM Session.
        """
        return _get_previous_dagrun(task_instance=self, state=state, session=session)

    @provide_session
    def get_previous_ti(
        self,
        state: DagRunState | None = None,
        session: Session = NEW_SESSION,
    ) -> TaskInstance | None:
        """
        Return the task instance for the task that ran before this task instance.

        :param session: SQLAlchemy ORM Session
        :param state: If passed, it only take into account instances of a specific state.
        """
        return _get_previous_ti(task_instance=self, state=state, session=session)

    @provide_session
    def get_previous_logical_date(
        self,
        state: DagRunState | None = None,
        session: Session = NEW_SESSION,
    ) -> pendulum.DateTime | None:
        """
        Return the logical date from property previous_ti_success.

        :param state: If passed, it only take into account instances of a specific state.
        :param session: SQLAlchemy ORM Session
        """
        return _get_previous_logical_date(task_instance=self, state=state, session=session)

    @provide_session
    def get_previous_start_date(
        self, state: DagRunState | None = None, session: Session = NEW_SESSION
    ) -> pendulum.DateTime | None:
        """
        Return the start date from property previous_ti_success.

        :param state: If passed, it only take into account instances of a specific state.
        :param session: SQLAlchemy ORM Session
        """
        return _get_previous_start_date(task_instance=self, state=state, session=session)

    @provide_session
    def are_dependencies_met(
        self, dep_context: DepContext | None = None, session: Session = NEW_SESSION, verbose: bool = False
    ) -> bool:
        """
        Are all conditions met for this task instance to be run given the context for the dependencies.

        (e.g. a task instance being force run from the UI will ignore some dependencies).

        :param dep_context: The execution context that determines the dependencies that should be evaluated.
        :param session: database session
        :param verbose: whether log details on failed dependencies on info or debug log level
        """
        dep_context = dep_context or DepContext()
        failed = False
        verbose_aware_logger = self.log.info if verbose else self.log.debug
        for dep_status in self.get_failed_dep_statuses(dep_context=dep_context, session=session):
            failed = True

            verbose_aware_logger(
                "Dependencies not met for %s, dependency '%s' FAILED: %s",
                self,
                dep_status.dep_name,
                dep_status.reason,
            )

        if failed:
            return False

        verbose_aware_logger("Dependencies all met for dep_context=%s ti=%s", dep_context.description, self)
        return True

    @provide_session
    def get_failed_dep_statuses(self, dep_context: DepContext | None = None, session: Session = NEW_SESSION):
        """Get failed Dependencies."""
        if TYPE_CHECKING:
            assert isinstance(self.task, BaseOperator)

        if not hasattr(self.task, "deps"):
            # These deps are not on BaseOperator since they are only needed and evaluated
            # in the scheduler and not needed at the Runtime.
            from airflow.serialization.serialized_objects import SerializedBaseOperator

            serialized_op = SerializedBaseOperator.deserialize_operator(
                SerializedBaseOperator.serialize_operator(self.task)
            )
            setattr(self.task, "deps", serialized_op.deps)  # type: ignore[union-attr]

        dep_context = dep_context or DepContext()
        for dep in dep_context.deps | self.task.deps:
            for dep_status in dep.get_dep_statuses(self, session, dep_context):
                self.log.debug(
                    "%s dependency '%s' PASSED: %s, %s",
                    self,
                    dep_status.dep_name,
                    dep_status.passed,
                    dep_status.reason,
                )

                if not dep_status.passed:
                    yield dep_status

    def __repr__(self) -> str:
        prefix = f"<TaskInstance: {self.dag_id}.{self.task_id} {self.run_id} "
        if self.map_index != -1:
            prefix += f"map_index={self.map_index} "
        return prefix + f"[{self.state}]>"

    def next_retry_datetime(self):
        """
        Get datetime of the next retry if the task instance fails.

        For exponential backoff, retry_delay is used as base and will be converted to seconds.
        """
        from airflow.sdk.definitions._internal.abstractoperator import MAX_RETRY_DELAY

        delay = self.task.retry_delay
        if self.task.retry_exponential_backoff:
            # If the min_backoff calculation is below 1, it will be converted to 0 via int. Thus,
            # we must round up prior to converting to an int, otherwise a divide by zero error
            # will occur in the modded_hash calculation.
            # this probably gives unexpected results if a task instance has previously been cleared,
            # because try_number can increase without bound
            min_backoff = math.ceil(delay.total_seconds() * (2 ** (self.try_number - 1)))

            # In the case when delay.total_seconds() is 0, min_backoff will not be rounded up to 1.
            # To address this, we impose a lower bound of 1 on min_backoff. This effectively makes
            # the ceiling function unnecessary, but the ceiling function was retained to avoid
            # introducing a breaking change.
            if min_backoff < 1:
                min_backoff = 1

            # deterministic per task instance
            ti_hash = int(
                hashlib.sha1(
                    f"{self.dag_id}#{self.task_id}#{self.logical_date}#{self.try_number}".encode(),
                    usedforsecurity=False,
                ).hexdigest(),
                16,
            )
            # between 1 and 1.0 * delay * (2^retry_number)
            modded_hash = min_backoff + ti_hash % min_backoff
            # timedelta has a maximum representable value. The exponentiation
            # here means this value can be exceeded after a certain number
            # of tries (around 50 if the initial delay is 1s, even fewer if
            # the delay is larger). Cap the value here before creating a
            # timedelta object so the operation doesn't fail with "OverflowError".
            delay_backoff_in_seconds = min(modded_hash, MAX_RETRY_DELAY)
            delay = timedelta(seconds=delay_backoff_in_seconds)
            if self.task.max_retry_delay:
                delay = min(self.task.max_retry_delay, delay)
        return self.end_date + delay

    def ready_for_retry(self) -> bool:
        """Check on whether the task instance is in the right state and timeframe to be retried."""
        return self.state == TaskInstanceState.UP_FOR_RETRY and self.next_retry_datetime() < timezone.utcnow()

    @staticmethod
    def _get_dagrun(dag_id, run_id, session) -> DagRun:
        from airflow.models.dagrun import DagRun  # Avoid circular import

        dr = session.query(DagRun).filter(DagRun.dag_id == dag_id, DagRun.run_id == run_id).one()
        return dr

    @provide_session
    def get_dagrun(self, session: Session = NEW_SESSION) -> DagRun:
        """
        Return the DagRun for this TaskInstance.

        :param session: SQLAlchemy ORM Session
        :return: DagRun
        """
        info = inspect(self)
        if info.attrs.dag_run.loaded_value is not NO_VALUE:
            if getattr(self, "task", None) is not None:
                if TYPE_CHECKING:
                    assert self.task
                self.dag_run.dag = self.task.dag
            return self.dag_run

        dr = self._get_dagrun(self.dag_id, self.run_id, session)
        if getattr(self, "task", None) is not None:
            if TYPE_CHECKING:
                assert self.task
                assert isinstance(self.task.dag, SchedulerDAG)
            dr.dag = self.task.dag
        # Record it in the instance for next time. This means that `self.logical_date` will work correctly
        set_committed_value(self, "dag_run", dr)

        return dr

    @classmethod
    @provide_session
    def ensure_dag(cls, task_instance: TaskInstance, session: Session = NEW_SESSION) -> DAG:
        """Ensure that task has a dag object associated, might have been removed by serialization."""
        if TYPE_CHECKING:
            assert task_instance.task
        if task_instance.task.dag is None:
            task_instance.task.dag = DagBag(read_dags_from_db=True).get_dag(
                dag_id=task_instance.dag_id, session=session
            )
        if TYPE_CHECKING:
            assert task_instance.task.dag
        return task_instance.task.dag

    @classmethod
    @provide_session
    def _check_and_change_state_before_execution(
        cls,
        task_instance: TaskInstance,
        verbose: bool = True,
        ignore_all_deps: bool = False,
        ignore_depends_on_past: bool = False,
        wait_for_past_depends_before_skipping: bool = False,
        ignore_task_deps: bool = False,
        ignore_ti_state: bool = False,
        mark_success: bool = False,
        test_mode: bool = False,
        hostname: str = "",
        pool: str | None = None,
        external_executor_id: str | None = None,
        session: Session = NEW_SESSION,
    ) -> bool:
        """
        Check dependencies and then sets state to RUNNING if they are met.

        Returns True if and only if state is set to RUNNING, which implies that task should be
        executed, in preparation for _run_raw_task.

        :param verbose: whether to turn on more verbose logging
        :param ignore_all_deps: Ignore all of the non-critical dependencies, just runs
        :param ignore_depends_on_past: Ignore depends_on_past DAG attribute
        :param wait_for_past_depends_before_skipping: Wait for past depends before mark the ti as skipped
        :param ignore_task_deps: Don't check the dependencies of this TaskInstance's task
        :param ignore_ti_state: Disregards previous task instance state
        :param mark_success: Don't run the task, mark its state as success
        :param test_mode: Doesn't record success or failure in the DB
        :param hostname: The hostname of the worker running the task instance.
        :param pool: specifies the pool to use to run the task instance
        :param external_executor_id: The identifier of the celery executor
        :param session: SQLAlchemy ORM Session
        :return: whether the state was changed to running or not
        """
        if TYPE_CHECKING:
            assert task_instance.task

        ti: TaskInstance = task_instance
        task = task_instance.task
        if TYPE_CHECKING:
            assert task
        ti.refresh_from_task(task, pool_override=pool)
        ti.test_mode = test_mode
        ti.refresh_from_db(session=session, lock_for_update=True)
        ti.hostname = hostname
        ti.pid = None

        if not ignore_all_deps and not ignore_ti_state and ti.state == TaskInstanceState.SUCCESS:
            Stats.incr("previously_succeeded", tags=ti.stats_tags)

        if not mark_success:
            # Firstly find non-runnable and non-requeueable tis.
            # Since mark_success is not set, we do nothing.
            non_requeueable_dep_context = DepContext(
                deps=RUNNING_DEPS - REQUEUEABLE_DEPS,
                ignore_all_deps=ignore_all_deps,
                ignore_ti_state=ignore_ti_state,
                ignore_depends_on_past=ignore_depends_on_past,
                wait_for_past_depends_before_skipping=wait_for_past_depends_before_skipping,
                ignore_task_deps=ignore_task_deps,
                description="non-requeueable deps",
            )
            if not ti.are_dependencies_met(
                dep_context=non_requeueable_dep_context, session=session, verbose=True
            ):
                session.commit()
                return False

            # For reporting purposes, we report based on 1-indexed,
            # not 0-indexed lists (i.e. Attempt 1 instead of
            # Attempt 0 for the first attempt).
            # Set the task start date. In case it was re-scheduled use the initial
            # start date that is recorded in task_reschedule table
            # If the task continues after being deferred (next_method is set), use the original start_date
            ti.start_date = ti.start_date if ti.next_method else timezone.utcnow()
            if ti.state == TaskInstanceState.UP_FOR_RESCHEDULE:
                tr_start_date = session.scalar(
                    TR.stmt_for_task_instance(ti, descending=False).with_only_columns(TR.start_date).limit(1)
                )
                if tr_start_date:
                    ti.start_date = tr_start_date

            # Secondly we find non-runnable but requeueable tis. We reset its state.
            # This is because we might have hit concurrency limits,
            # e.g. because of backfilling.
            dep_context = DepContext(
                deps=REQUEUEABLE_DEPS,
                ignore_all_deps=ignore_all_deps,
                ignore_depends_on_past=ignore_depends_on_past,
                wait_for_past_depends_before_skipping=wait_for_past_depends_before_skipping,
                ignore_task_deps=ignore_task_deps,
                ignore_ti_state=ignore_ti_state,
                description="requeueable deps",
            )
            if not ti.are_dependencies_met(dep_context=dep_context, session=session, verbose=True):
                ti.state = None
                cls.logger().warning(
                    "Rescheduling due to concurrency limits reached "
                    "at task runtime. Attempt %s of "
                    "%s. State set to NONE.",
                    ti.try_number,
                    ti.max_tries + 1,
                )
                ti.queued_dttm = timezone.utcnow()
                session.merge(ti)
                session.commit()
                return False

        if ti.next_kwargs is not None:
            cls.logger().info("Resuming after deferral")
        else:
            cls.logger().info("Starting attempt %s of %s", ti.try_number, ti.max_tries + 1)

        if not test_mode:
            session.add(Log(TaskInstanceState.RUNNING.value, ti))

        ti.state = TaskInstanceState.RUNNING
        ti.emit_state_change_metric(TaskInstanceState.RUNNING)

        if external_executor_id:
            ti.external_executor_id = external_executor_id

        ti.end_date = None
        if not test_mode:
            session.merge(ti).task = task
        session.commit()

        # Closing all pooled connections to prevent
        # "max number of connections reached"
        settings.engine.dispose()  # type: ignore
        if verbose:
            if mark_success:
                cls.logger().info("Marking success for %s on %s", ti.task, ti.logical_date)
            else:
                cls.logger().info("Executing %s on %s", ti.task, ti.logical_date)
        return True

    @provide_session
    def check_and_change_state_before_execution(
        self,
        verbose: bool = True,
        ignore_all_deps: bool = False,
        ignore_depends_on_past: bool = False,
        wait_for_past_depends_before_skipping: bool = False,
        ignore_task_deps: bool = False,
        ignore_ti_state: bool = False,
        mark_success: bool = False,
        test_mode: bool = False,
        pool: str | None = None,
        external_executor_id: str | None = None,
        session: Session = NEW_SESSION,
    ) -> bool:
        return TaskInstance._check_and_change_state_before_execution(
            task_instance=self,
            verbose=verbose,
            ignore_all_deps=ignore_all_deps,
            ignore_depends_on_past=ignore_depends_on_past,
            wait_for_past_depends_before_skipping=wait_for_past_depends_before_skipping,
            ignore_task_deps=ignore_task_deps,
            ignore_ti_state=ignore_ti_state,
            mark_success=mark_success,
            test_mode=test_mode,
            hostname=get_hostname(),
            pool=pool,
            external_executor_id=external_executor_id,
            session=session,
        )

    def emit_state_change_metric(self, new_state: TaskInstanceState) -> None:
        """
        Send a time metric representing how much time a given state transition took.

        The previous state and metric name is deduced from the state the task was put in.

        :param new_state: The state that has just been set for this task.
            We do not use `self.state`, because sometimes the state is updated directly in the DB and not in
            the local TaskInstance object.
            Supported states: QUEUED and RUNNING
        """
        if self.end_date:
            # if the task has an end date, it means that this is not its first round.
            # we send the state transition time metric only on the first try, otherwise it gets more complex.
            return

        # switch on state and deduce which metric to send
        if new_state == TaskInstanceState.RUNNING:
            metric_name = "queued_duration"
            if self.queued_dttm is None:
                # this should not really happen except in tests or rare cases,
                # but we don't want to create errors just for a metric, so we just skip it
                self.log.warning(
                    "cannot record %s for task %s because previous state change time has not been saved",
                    metric_name,
                    self.task_id,
                )
                return
            timing = timezone.utcnow() - self.queued_dttm
        elif new_state == TaskInstanceState.QUEUED:
            metric_name = "scheduled_duration"
            if self.scheduled_dttm is None:
                self.log.warning(
                    "cannot record %s for task %s because previous state change time has not been saved",
                    metric_name,
                    self.task_id,
                )
                return
            timing = timezone.utcnow() - self.scheduled_dttm
        else:
            raise NotImplementedError("no metric emission setup for state %s", new_state)

        # send metric twice, once (legacy) with tags in the name and once with tags as tags
        Stats.timing(f"dag.{self.dag_id}.{self.task_id}.{metric_name}", timing)
        Stats.timing(
            f"task.{metric_name}",
            timing,
            tags={"task_id": self.task_id, "dag_id": self.dag_id, "queue": self.queue},
        )

    def clear_next_method_args(self) -> None:
        """Ensure we unset next_method and next_kwargs to ensure that any retries don't reuse them."""
        _clear_next_method_args(task_instance=self)

    @provide_session
    @Sentry.enrich_errors
    def _run_raw_task(
        self,
        mark_success: bool = False,
        test_mode: bool = False,
        pool: str | None = None,
        raise_on_defer: bool = False,
        session: Session = NEW_SESSION,
    ) -> TaskReturnCode | None:
        """
        Run a task, update the state upon completion, and run any appropriate callbacks.

        Immediately runs the task (without checking or changing db state
        before execution) and then sets the appropriate final state after
        completion and runs any post-execute callbacks. Meant to be called
        only after another function changes the state to running.

        :param mark_success: Don't run the task, mark its state as success
        :param test_mode: Doesn't record success or failure in the DB
        :param pool: specifies the pool to use to run the task instance
        :param session: SQLAlchemy ORM Session
        """
        if TYPE_CHECKING:
            assert self.task

        return _run_raw_task(
            ti=self,
            mark_success=mark_success,
            test_mode=test_mode,
            pool=pool,
            raise_on_defer=raise_on_defer,
            session=session,
        )

    @staticmethod
    @provide_session
    def register_asset_changes_in_db(
        ti: TaskInstance,
        task_outlets: list[AssetProfile],
        outlet_events: list[dict[str, Any]],
        session: Session = NEW_SESSION,
    ) -> None:
        from airflow.sdk.definitions.asset import Asset, AssetAlias, AssetNameRef, AssetUniqueKey, AssetUriRef

        asset_keys = {
            AssetUniqueKey(o.name, o.uri)
            for o in task_outlets
            if o.type == Asset.__name__ and o.name and o.uri
        }
        asset_name_refs = {
            Asset.ref(name=o.name) for o in task_outlets if o.type == AssetNameRef.__name__ and o.name
        }
        asset_uri_refs = {
            Asset.ref(uri=o.uri) for o in task_outlets if o.type == AssetUriRef.__name__ and o.uri
        }

        asset_models: dict[AssetUniqueKey, AssetModel] = {
            AssetUniqueKey.from_asset(am): am
            for am in session.scalars(
                select(AssetModel).where(
                    AssetModel.active.has(),
                    or_(
                        tuple_(AssetModel.name, AssetModel.uri).in_(attrs.astuple(k) for k in asset_keys),
                        AssetModel.name.in_(r.name for r in asset_name_refs),
                        AssetModel.uri.in_(r.uri for r in asset_uri_refs),
                    ),
                )
            )
        }

        asset_event_extras: dict[AssetUniqueKey, dict] = {
            AssetUniqueKey(**event["dest_asset_key"]): event["extra"]
            for event in outlet_events
            if "source_alias_name" not in event
        }

        bad_asset_keys: set[AssetUniqueKey | AssetNameRef | AssetUriRef] = set()

        for key in asset_keys:
            try:
                am = asset_models[key]
            except KeyError:
                bad_asset_keys.add(key)
                continue
            ti.log.debug("register event for asset %s", am)
            asset_manager.register_asset_change(
                task_instance=ti,
                asset=am,
                extra=asset_event_extras.get(key),
                session=session,
            )

        if asset_name_refs:
            asset_models_by_name = {key.name: am for key, am in asset_models.items()}
            asset_event_extras_by_name = {key.name: extra for key, extra in asset_event_extras.items()}
            for nref in asset_name_refs:
                try:
                    am = asset_models_by_name[nref.name]
                except KeyError:
                    bad_asset_keys.add(nref)
                    continue
                ti.log.debug("register event for asset name ref %s", am)
                asset_manager.register_asset_change(
                    task_instance=ti,
                    asset=am,
                    extra=asset_event_extras_by_name.get(nref.name),
                    session=session,
                )
        if asset_uri_refs:
            asset_models_by_uri = {key.uri: am for key, am in asset_models.items()}
            asset_event_extras_by_uri = {key.uri: extra for key, extra in asset_event_extras.items()}
            for uref in asset_uri_refs:
                try:
                    am = asset_models_by_uri[uref.uri]
                except KeyError:
                    bad_asset_keys.add(uref)
                    continue
                ti.log.debug("register event for asset uri ref %s", am)
                asset_manager.register_asset_change(
                    task_instance=ti,
                    asset=am,
                    extra=asset_event_extras_by_uri.get(uref.uri),
                    session=session,
                )

        def _asset_event_extras_from_aliases() -> dict[tuple[AssetUniqueKey, frozenset], set[str]]:
            d = defaultdict(set)
            for event in outlet_events:
                try:
                    alias_name = event["source_alias_name"]
                except KeyError:
                    continue
                if alias_name not in outlet_alias_names:
                    continue
                asset_key = AssetUniqueKey(**event["dest_asset_key"])
                extra_key = frozenset(event["extra"].items())
                d[asset_key, extra_key].add(alias_name)
            return d

        outlet_alias_names = {o.name for o in task_outlets if o.type == AssetAlias.__name__ and o.name}
        if outlet_alias_names and (event_extras_from_aliases := _asset_event_extras_from_aliases()):
            for (asset_key, extra_key), event_aliase_names in event_extras_from_aliases.items():
                ti.log.debug("register event for asset %s with aliases %s", asset_key, event_aliase_names)
                event = asset_manager.register_asset_change(
                    task_instance=ti,
                    asset=asset_key,
                    source_alias_names=event_aliase_names,
                    extra=dict(extra_key),
                    session=session,
                )
                if event is None:
                    ti.log.info("Dynamically creating AssetModel %s", asset_key)
                    session.add(AssetModel(name=asset_key.name, uri=asset_key.uri))
                    session.flush()  # So event can set up its asset fk.
                    asset_manager.register_asset_change(
                        task_instance=ti,
                        asset=asset_key,
                        source_alias_names=event_aliase_names,
                        extra=dict(extra_key),
                        session=session,
                    )

        if bad_asset_keys:
            raise AirflowInactiveAssetInInletOrOutletException(bad_asset_keys)

    def _execute_task_with_callbacks(self, context: Context, test_mode: bool = False, *, session: Session):
        """Prepare Task for Execution."""
        from airflow.sdk.execution_time.callback_runner import create_executable_runner
        from airflow.sdk.execution_time.context import context_get_outlet_events

        if TYPE_CHECKING:
            assert self.task

        parent_pid = os.getpid()

        def signal_handler(signum, frame):
            pid = os.getpid()

            # If a task forks during execution (from DAG code) for whatever
            # reason, we want to make sure that we react to the signal only in
            # the process that we've spawned ourselves (referred to here as the
            # parent process).
            if pid != parent_pid:
                os._exit(1)
                return
            self.log.error("Received SIGTERM. Terminating subprocesses.")
            self.log.error("Stacktrace: \n%s", "".join(traceback.format_stack()))
            self.task.on_kill()
            raise AirflowTaskTerminated(
                f"Task received SIGTERM signal {self.task_id=} {self.dag_id=} {self.run_id=} {self.map_index=}"
            )

        signal.signal(signal.SIGTERM, signal_handler)

        # Don't clear Xcom until the task is certain to execute, and check if we are resuming from deferral.
        if not self.next_method:
            self.clear_xcom_data()

        with (
            Stats.timer(f"dag.{self.task.dag_id}.{self.task.task_id}.duration"),
            Stats.timer("task.duration", tags=self.stats_tags),
        ):
            # Set the validated/merged params on the task object.
            self.task.params = context["params"]

            with set_current_context(context):
                dag = self.task.get_dag()
                if dag is not None:
                    jinja_env = dag.get_template_env()
                else:
                    jinja_env = None
                task_orig = self.render_templates(context=context, jinja_env=jinja_env)

            # The task is never MappedOperator at this point.
            if TYPE_CHECKING:
                assert isinstance(self.task, BaseOperator)

            if not test_mode:
                rendered_fields = get_serialized_template_fields(task=self.task)
                _update_rtif(ti=self, rendered_fields=rendered_fields)
            # Export context to make it available for operators to use.
            airflow_context_vars = context_to_airflow_vars(context, in_env_var_format=True)
            os.environ.update(airflow_context_vars)

            # Log context only for the default execution method, the assumption
            # being that otherwise we're resuming a deferred task (in which
            # case there's no need to log these again).
            if not self.next_method:
                self.log.info(
                    "Exporting env vars: %s",
                    " ".join(f"{k}={v!r}" for k, v in airflow_context_vars.items()),
                )

            # Run pre_execute callback
            if self.task._pre_execute_hook:
                create_executable_runner(
                    self.task._pre_execute_hook,
                    context_get_outlet_events(context),
                    logger=self.log,
                ).run(context)
            create_executable_runner(
                self.task.pre_execute,
                context_get_outlet_events(context),
                logger=self.log,
            ).run(context)

            # Run on_execute callback
            self._run_execute_callback(context, self.task)

            # Run on_task_instance_running event
            try:
                get_listener_manager().hook.on_task_instance_running(
                    previous_state=TaskInstanceState.QUEUED, task_instance=self
                )
            except Exception:
                log.exception("error calling listener")

            def _render_map_index(context: Context, *, jinja_env: jinja2.Environment | None) -> str | None:
                """Render named map index if the DAG author defined map_index_template at the task level."""
                if jinja_env is None or (template := context.get("map_index_template")) is None:
                    return None
                rendered_map_index = jinja_env.from_string(template).render(context)
                log.debug("Map index rendered as %s", rendered_map_index)
                return rendered_map_index

            # Execute the task.
            with set_current_context(context):
                try:
                    result = self._execute_task(context, task_orig)
                except Exception:
                    # If the task failed, swallow rendering error so it doesn't mask the main error.
                    with contextlib.suppress(jinja2.TemplateSyntaxError, jinja2.UndefinedError):
                        self._rendered_map_index = _render_map_index(context, jinja_env=jinja_env)
                    raise
                else:  # If the task succeeded, render normally to let rendering error bubble up.
                    self._rendered_map_index = _render_map_index(context, jinja_env=jinja_env)

            # Run post_execute callback
            if self.task._post_execute_hook:
                create_executable_runner(
                    self.task._post_execute_hook,
                    context_get_outlet_events(context),
                    logger=self.log,
                ).run(context, result)
            create_executable_runner(
                self.task.post_execute,
                context_get_outlet_events(context),
                logger=self.log,
            ).run(context, result)

        Stats.incr(f"operator_successes_{self.task.task_type}", tags=self.stats_tags)
        # Same metric with tagging
        Stats.incr("operator_successes", tags={**self.stats_tags, "task_type": self.task.task_type})
        Stats.incr("ti_successes", tags=self.stats_tags)

    def _execute_task(self, context: Context, task_orig: Operator):
        """
        Execute Task (optionally with a Timeout) and push Xcom results.

        :param context: Jinja2 context
        :param task_orig: origin task
        """
        return _execute_task(self, context, task_orig)

    def update_heartbeat(self):
        cm = create_session()
        with cm as session_or_null:
            _update_ti_heartbeat(self.id, timezone.utcnow(), session_or_null)

    @provide_session
    def defer_task(self, exception: TaskDeferred | None, session: Session = NEW_SESSION) -> None:
        """
        Mark the task as deferred and sets up the trigger that is needed to resume it when TaskDeferred is raised.

        :meta: private
        """
        _defer_task(ti=self, exception=exception, session=session)

    def _run_execute_callback(self, context: Context, task: BaseOperator) -> None:
        """Functions that need to be run before a Task is executed."""
        if not (callbacks := task.on_execute_callback):
            return
        for callback in callbacks if isinstance(callbacks, list) else [callbacks]:
            try:
                callback(context)
            except Exception:
                self.log.exception("Failed when executing execute callback")

    @provide_session
    def run(
        self,
        verbose: bool = True,
        ignore_all_deps: bool = False,
        ignore_depends_on_past: bool = False,
        wait_for_past_depends_before_skipping: bool = False,
        ignore_task_deps: bool = False,
        ignore_ti_state: bool = False,
        mark_success: bool = False,
        test_mode: bool = False,
        pool: str | None = None,
        session: Session = NEW_SESSION,
        raise_on_defer: bool = False,
    ) -> None:
        """Run TaskInstance."""
        res = self.check_and_change_state_before_execution(
            verbose=verbose,
            ignore_all_deps=ignore_all_deps,
            ignore_depends_on_past=ignore_depends_on_past,
            wait_for_past_depends_before_skipping=wait_for_past_depends_before_skipping,
            ignore_task_deps=ignore_task_deps,
            ignore_ti_state=ignore_ti_state,
            mark_success=mark_success,
            test_mode=test_mode,
            pool=pool,
            session=session,
        )
        if not res:
            return

        self._run_raw_task(
            mark_success=mark_success,
            test_mode=test_mode,
            pool=pool,
            session=session,
            raise_on_defer=raise_on_defer,
        )

    def dry_run(self) -> None:
        """Only Renders Templates for the TI."""
        if TYPE_CHECKING:
            assert self.task

        self.task = self.task.prepare_for_execution()
        self.render_templates()
        if TYPE_CHECKING:
            assert isinstance(self.task, BaseOperator)
        self.task.dry_run()

    @provide_session
    def _handle_reschedule(
        self,
        actual_start_date: datetime,
        reschedule_exception: AirflowRescheduleException,
        test_mode: bool = False,
        session: Session = NEW_SESSION,
    ):
        _handle_reschedule(
            ti=self,
            actual_start_date=actual_start_date,
            reschedule_exception=reschedule_exception,
            test_mode=test_mode,
            session=session,
        )

    @staticmethod
    def get_truncated_error_traceback(error: BaseException, truncate_to: Callable) -> TracebackType | None:
        """
        Truncate the traceback of an exception to the first frame called from within a given function.

        :param error: exception to get traceback from
        :param truncate_to: Function to truncate TB to. Must have a ``__code__`` attribute

        :meta private:
        """
        tb = error.__traceback__
        code = truncate_to.__func__.__code__  # type: ignore[attr-defined]
        while tb is not None:
            if tb.tb_frame.f_code is code:
                return tb.tb_next
            tb = tb.tb_next
        return tb or error.__traceback__

    @classmethod
    def fetch_handle_failure_context(
        cls,
        ti: TaskInstance,
        error: None | str | BaseException,
        test_mode: bool | None = None,
        context: Context | None = None,
        force_fail: bool = False,
        *,
        session: Session,
        fail_fast: bool = False,
    ):
        """
        Fetch the context needed to handle a failure.

        :param ti: TaskInstance
        :param error: if specified, log the specific exception if thrown
        :param test_mode: doesn't record success or failure in the DB if True
        :param context: Jinja2 context
        :param force_fail: if True, task does not retry
        :param session: SQLAlchemy ORM Session
        :param fail_fast: if True, fail all downstream tasks
        """
        if error:
            if isinstance(error, BaseException):
                tb = TaskInstance.get_truncated_error_traceback(error, truncate_to=ti._execute_task)
                cls.logger().error("Task failed with exception", exc_info=(type(error), error, tb))
            else:
                cls.logger().error("%s", error)
        if not test_mode:
            ti.refresh_from_db(session)

        ti.end_date = timezone.utcnow()
        ti.set_duration()

        Stats.incr(f"operator_failures_{ti.operator}", tags=ti.stats_tags)
        # Same metric with tagging
        Stats.incr("operator_failures", tags={**ti.stats_tags, "operator": ti.operator})
        Stats.incr("ti_failures", tags=ti.stats_tags)

        if not test_mode:
            session.add(Log(TaskInstanceState.FAILED.value, ti))

        ti.clear_next_method_args()

        # In extreme cases (task instance heartbeat timeout in case of dag with parse error) we might _not_ have a Task.
        if context is None and getattr(ti, "task", None):
            context = ti.get_template_context(session)

        if context is not None:
            context["exception"] = error

        # Set state correctly and figure out how to log it and decide whether
        # to email

        # Note, callback invocation needs to be handled by caller of
        # _run_raw_task to avoid race conditions which could lead to duplicate
        # invocations or miss invocation.

        # Since this function is called only when the TaskInstance state is running,
        # try_number contains the current try_number (not the next). We
        # only mark task instance as FAILED if the next task instance
        # try_number exceeds the max_tries ... or if force_fail is truthy

        task: BaseOperator | None = None
        try:
            if getattr(ti, "task", None) and context:
                if TYPE_CHECKING:
                    assert isinstance(ti.task, BaseOperator)
                task = ti.task.unmap((context, session))
        except Exception:
            cls.logger().error("Unable to unmap task to determine if we need to send an alert email")

        if force_fail or not ti.is_eligible_to_retry():
            ti.state = TaskInstanceState.FAILED
            email_for_state = operator.attrgetter("email_on_failure")
            callbacks = task.on_failure_callback if task else None

            if task and fail_fast:
                _stop_remaining_tasks(task_instance=ti, session=session)
        else:
            if ti.state == TaskInstanceState.RUNNING:
                # If the task instance is in the running state, it means it raised an exception and
                # about to retry so we record the task instance history. For other states, the task
                # instance was cleared and already recorded in the task instance history.
                from airflow.models.taskinstancehistory import TaskInstanceHistory

                TaskInstanceHistory.record_ti(ti, session=session)
                ti.try_id = uuid7()

            ti.state = State.UP_FOR_RETRY
            email_for_state = operator.attrgetter("email_on_retry")
            callbacks = task.on_retry_callback if task else None

        try:
            get_listener_manager().hook.on_task_instance_failed(
                previous_state=TaskInstanceState.RUNNING, task_instance=ti, error=error
            )
        except Exception:
            log.exception("error calling listener")

        return {
            "ti": ti,
            "email_for_state": email_for_state,
            "task": task,
            "callbacks": callbacks,
            "context": context,
        }

    @staticmethod
    @provide_session
    def save_to_db(ti: TaskInstance, session: Session = NEW_SESSION):
        ti.updated_at = timezone.utcnow()
        session.merge(ti)
        session.flush()
        session.commit()

    @provide_session
    def handle_failure(
        self,
        error: None | str | BaseException,
        test_mode: bool | None = None,
        context: Context | None = None,
        force_fail: bool = False,
        session: Session = NEW_SESSION,
    ) -> None:
        """
        Handle Failure for a task instance.

        :param error: if specified, log the specific exception if thrown
        :param session: SQLAlchemy ORM Session
        :param test_mode: doesn't record success or failure in the DB if True
        :param context: Jinja2 context
        :param force_fail: if True, task does not retry
        """
        if TYPE_CHECKING:
            assert self.task
            assert self.task.dag
        try:
            fail_fast = self.task.dag.fail_fast
        except Exception:
            fail_fast = False
        _handle_failure(
            task_instance=self,
            error=error,
            session=session,
            test_mode=test_mode,
            context=context,
            force_fail=force_fail,
            fail_fast=fail_fast,
        )

    def is_eligible_to_retry(self) -> bool:
        """Is task instance is eligible for retry."""
        return _is_eligible_to_retry(task_instance=self)

    def get_template_context(
        self,
        session: Session | None = None,
        ignore_param_exceptions: bool = True,
    ) -> Context:
        """
        Return TI Context.

        :param session: SQLAlchemy ORM Session
        :param ignore_param_exceptions: flag to suppress value exceptions while initializing the ParamsDict
        """
        if TYPE_CHECKING:
            assert self.task
            assert isinstance(self.task.dag, SchedulerDAG)
        return _get_template_context(
            task_instance=self,
            dag=self.task.dag,
            session=session,
            ignore_param_exceptions=ignore_param_exceptions,
        )

    @provide_session
    def get_rendered_template_fields(self, session: Session = NEW_SESSION) -> None:
        """
        Update task with rendered template fields for presentation in UI.

        If task has already run, will fetch from DB; otherwise will render.
        """
        from airflow.models.renderedtifields import RenderedTaskInstanceFields

        if TYPE_CHECKING:
            assert isinstance(self.task, BaseOperator)

        rendered_task_instance_fields = RenderedTaskInstanceFields.get_templated_fields(self, session=session)
        if rendered_task_instance_fields:
            self.task = self.task.unmap(None)
            for field_name, rendered_value in rendered_task_instance_fields.items():
                setattr(self.task, field_name, rendered_value)
            return

        try:
            # If we get here, either the task hasn't run or the RTIF record was purged.
            from airflow.sdk.execution_time.secrets_masker import redact

            self.render_templates()
            for field_name in self.task.template_fields:
                rendered_value = getattr(self.task, field_name)
                setattr(self.task, field_name, redact(rendered_value, field_name))
        except (TemplateAssertionError, UndefinedError) as e:
            raise AirflowException(
                "Webserver does not have access to User-defined Macros or Filters "
                "when Dag Serialization is enabled. Hence for the task that have not yet "
                "started running, please use 'airflow tasks render' for debugging the "
                "rendering of template_fields."
            ) from e

    def overwrite_params_with_dag_run_conf(self, params: dict, dag_run: DagRun):
        """Overwrite Task Params with DagRun.conf."""
        if dag_run and dag_run.conf:
            self.log.debug("Updating task params (%s) with DagRun.conf (%s)", params, dag_run.conf)
            params.update(dag_run.conf)

    def render_templates(
        self, context: Context | None = None, jinja_env: jinja2.Environment | None = None
    ) -> Operator:
        """
        Render templates in the operator fields.

        If the task was originally mapped, this may replace ``self.task`` with
        the unmapped, fully rendered BaseOperator. The original ``self.task``
        before replacement is returned.
        """
        from airflow.sdk.definitions.mappedoperator import MappedOperator

        if not context:
            context = self.get_template_context()
        original_task = self.task

        ti = context["ti"]

        if TYPE_CHECKING:
            assert original_task
            assert self.task
            assert ti.task

        # If self.task is mapped, this call replaces self.task to point to the
        # unmapped BaseOperator created by this function! This is because the
        # MappedOperator is useless for template rendering, and we need to be
        # able to access the unmapped task instead.
        original_task.render_template_fields(context, jinja_env)
        if isinstance(self.task, MappedOperator):
            self.task = context["ti"].task  # type: ignore[assignment]

        return original_task

    def get_email_subject_content(
        self, exception: BaseException, task: BaseOperator | None = None
    ) -> tuple[str, str, str]:
        """
        Get the email subject content for exceptions.

        :param exception: the exception sent in the email
        :param task:
        """
        return _get_email_subject_content(task_instance=self, exception=exception, task=task)

    def email_alert(self, exception, task: BaseOperator) -> None:
        """
        Send alert email with exception information.

        :param exception: the exception
        :param task: task related to the exception
        """
        _email_alert(task_instance=self, exception=exception, task=task)

    def set_duration(self) -> None:
        """Set task instance duration."""
        _set_duration(task_instance=self)

    @provide_session
    def xcom_push(
        self,
        key: str,
        value: Any,
        session: Session = NEW_SESSION,
    ) -> None:
        """
        Make an XCom available for tasks to pull.

        :param key: Key to store the value under.
        :param value: Value to store. Only be JSON-serializable may be used otherwise.
        """
        XComModel.set(
            key=key,
            value=value,
            task_id=self.task_id,
            dag_id=self.dag_id,
            run_id=self.run_id,
            map_index=self.map_index,
            session=session,
        )

    @provide_session
    def xcom_pull(
        self,
        task_ids: str | Iterable[str] | None = None,
        dag_id: str | None = None,
        key: str = XCOM_RETURN_KEY,
        include_prior_dates: bool = False,
        session: Session = NEW_SESSION,
        *,
        map_indexes: int | Iterable[int] | None = None,
        default: Any = None,
        run_id: str | None = None,
    ) -> Any:
        """:meta private:"""  # noqa: D400
        # This is only kept for compatibility in tests for now while AIP-72 is in progress.
        return _xcom_pull(
            ti=self,
            task_ids=task_ids,
            dag_id=dag_id,
            key=key,
            include_prior_dates=include_prior_dates,
            session=session,
            map_indexes=map_indexes,
            default=default,
            run_id=run_id,
        )

    @provide_session
    def get_num_running_task_instances(self, session: Session, same_dagrun: bool = False) -> int:
        """Return Number of running TIs from the DB."""
        # .count() is inefficient
        num_running_task_instances_query = session.query(func.count()).filter(
            TaskInstance.dag_id == self.dag_id,
            TaskInstance.task_id == self.task_id,
            TaskInstance.state == TaskInstanceState.RUNNING,
        )
        if same_dagrun:
            num_running_task_instances_query = num_running_task_instances_query.filter(
                TaskInstance.run_id == self.run_id
            )
        return num_running_task_instances_query.scalar()

    @staticmethod
    def filter_for_tis(tis: Iterable[TaskInstance | TaskInstanceKey]) -> BooleanClauseList | None:
        """Return SQLAlchemy filter to query selected task instances."""
        # DictKeys type, (what we often pass here from the scheduler) is not directly indexable :(
        # Or it might be a generator, but we need to be able to iterate over it more than once
        tis = list(tis)

        if not tis:
            return None

        first = tis[0]

        dag_id = first.dag_id
        run_id = first.run_id
        map_index = first.map_index
        first_task_id = first.task_id

        # pre-compute the set of dag_id, run_id, map_indices and task_ids
        dag_ids, run_ids, map_indices, task_ids = set(), set(), set(), set()
        for t in tis:
            dag_ids.add(t.dag_id)
            run_ids.add(t.run_id)
            map_indices.add(t.map_index)
            task_ids.add(t.task_id)

        # Common path optimisations: when all TIs are for the same dag_id and run_id, or same dag_id
        # and task_id -- this can be over 150x faster for huge numbers of TIs (20k+)
        if dag_ids == {dag_id} and run_ids == {run_id} and map_indices == {map_index}:
            return and_(
                TaskInstance.dag_id == dag_id,
                TaskInstance.run_id == run_id,
                TaskInstance.map_index == map_index,
                TaskInstance.task_id.in_(task_ids),
            )
        if dag_ids == {dag_id} and task_ids == {first_task_id} and map_indices == {map_index}:
            return and_(
                TaskInstance.dag_id == dag_id,
                TaskInstance.run_id.in_(run_ids),
                TaskInstance.map_index == map_index,
                TaskInstance.task_id == first_task_id,
            )
        if dag_ids == {dag_id} and run_ids == {run_id} and task_ids == {first_task_id}:
            return and_(
                TaskInstance.dag_id == dag_id,
                TaskInstance.run_id == run_id,
                TaskInstance.map_index.in_(map_indices),
                TaskInstance.task_id == first_task_id,
            )

        filter_condition = []
        # create 2 nested groups, both primarily grouped by dag_id and run_id,
        # and in the nested group 1 grouped by task_id the other by map_index.
        task_id_groups: dict[tuple, dict[Any, list[Any]]] = defaultdict(lambda: defaultdict(list))
        map_index_groups: dict[tuple, dict[Any, list[Any]]] = defaultdict(lambda: defaultdict(list))
        for t in tis:
            task_id_groups[(t.dag_id, t.run_id)][t.task_id].append(t.map_index)
            map_index_groups[(t.dag_id, t.run_id)][t.map_index].append(t.task_id)

        # this assumes that most dags have dag_id as the largest grouping, followed by run_id. even
        # if its not, this is still  a significant optimization over querying for every single tuple key
        for cur_dag_id, cur_run_id in itertools.product(dag_ids, run_ids):
            # we compare the group size between task_id and map_index and use the smaller group
            dag_task_id_groups = task_id_groups[(cur_dag_id, cur_run_id)]
            dag_map_index_groups = map_index_groups[(cur_dag_id, cur_run_id)]

            if len(dag_task_id_groups) <= len(dag_map_index_groups):
                for cur_task_id, cur_map_indices in dag_task_id_groups.items():
                    filter_condition.append(
                        and_(
                            TaskInstance.dag_id == cur_dag_id,
                            TaskInstance.run_id == cur_run_id,
                            TaskInstance.task_id == cur_task_id,
                            TaskInstance.map_index.in_(cur_map_indices),
                        )
                    )
            else:
                for cur_map_index, cur_task_ids in dag_map_index_groups.items():
                    filter_condition.append(
                        and_(
                            TaskInstance.dag_id == cur_dag_id,
                            TaskInstance.run_id == cur_run_id,
                            TaskInstance.task_id.in_(cur_task_ids),
                            TaskInstance.map_index == cur_map_index,
                        )
                    )

        return or_(*filter_condition)

    @classmethod
    def ti_selector_condition(cls, vals: Collection[str | tuple[str, int]]) -> ColumnOperators:
        """
        Build an SQLAlchemy filter for a list of task_ids or tuples of (task_id,map_index).

        :meta private:
        """
        # Compute a filter for TI.task_id and TI.map_index based on input values
        # For each item, it will either be a task_id, or (task_id, map_index)
        task_id_only = [v for v in vals if isinstance(v, str)]
        with_map_index = [v for v in vals if not isinstance(v, str)]

        filters: list[ColumnOperators] = []
        if task_id_only:
            filters.append(cls.task_id.in_(task_id_only))
        if with_map_index:
            filters.append(tuple_(cls.task_id, cls.map_index).in_(with_map_index))

        if not filters:
            return false()
        if len(filters) == 1:
            return filters[0]
        return or_(*filters)

    def get_relevant_upstream_map_indexes(
        self,
        upstream: Operator,
        ti_count: int | None,
        *,
        session: Session,
    ) -> int | range | None:
        """
        Infer the map indexes of an upstream "relevant" to this ti.

        The bulk of the logic mainly exists to solve the problem described by
        the following example, where 'val' must resolve to different values,
        depending on where the reference is being used::

            @task
            def this_task(v):  # This is self.task.
                return v * 2


            @task_group
            def tg1(inp):
                val = upstream(inp)  # This is the upstream task.
                this_task(val)  # When inp is 1, val here should resolve to 2.
                return val


            # This val is the same object returned by tg1.
            val = tg1.expand(inp=[1, 2, 3])


            @task_group
            def tg2(inp):
                another_task(inp, val)  # val here should resolve to [2, 4, 6].


            tg2.expand(inp=["a", "b"])

        The surrounding mapped task groups of ``upstream`` and ``self.task`` are
        inspected to find a common "ancestor". If such an ancestor is found,
        we need to return specific map indexes to pull a partial value from
        upstream XCom.

        :param upstream: The referenced upstream task.
        :param ti_count: The total count of task instance this task was expanded
            by the scheduler, i.e. ``expanded_ti_count`` in the template context.
        :return: Specific map index or map indexes to pull, or ``None`` if we
            want to "whole" return value (i.e. no mapped task groups involved).
        """
        from airflow.models.baseoperator import BaseOperator

        if TYPE_CHECKING:
            assert self.task

        # This value should never be None since we already know the current task
        # is in a mapped task group, and should have been expanded, despite that,
        # we need to check that it is not None to satisfy Mypy.
        # But this value can be 0 when we expand an empty list, for that it is
        # necessary to check that ti_count is not 0 to avoid dividing by 0.
        if not ti_count:
            return None

        # Find the innermost common mapped task group between the current task
        # If the current task and the referenced task does not have a common
        # mapped task group, the two are in different task mapping contexts
        # (like another_task above), and we should use the "whole" value.
        common_ancestor = _find_common_ancestor_mapped_group(self.task, upstream)
        if common_ancestor is None:
            return None

        # At this point we know the two tasks share a mapped task group, and we
        # should use a "partial" value. Let's break down the mapped ti count
        # between the ancestor and further expansion happened inside it.

        ancestor_ti_count = BaseOperator.get_mapped_ti_count(common_ancestor, self.run_id, session=session)
        ancestor_map_index = self.map_index * ancestor_ti_count // ti_count

        # If the task is NOT further expanded inside the common ancestor, we
        # only want to reference one single ti. We must walk the actual DAG,
        # and "ti_count == ancestor_ti_count" does not work, since the further
        # expansion may be of length 1.
        if not _is_further_mapped_inside(upstream, common_ancestor):
            return ancestor_map_index

        # Otherwise we need a partial aggregation for values from selected task
        # instances in the ancestor's expansion context.
        further_count = ti_count // ancestor_ti_count
        map_index_start = ancestor_map_index * further_count
        return range(map_index_start, map_index_start + further_count)

    def clear_db_references(self, session: Session):
        """
        Clear db tables that have a reference to this instance.

        :param session: ORM Session

        :meta private:
        """
        from airflow.models.renderedtifields import RenderedTaskInstanceFields

        tables: list[type[TaskInstanceDependencies]] = [
            XComModel,
            RenderedTaskInstanceFields,
            TaskMap,
        ]
        tables_by_id: list[type[Base]] = [TaskInstanceNote, TaskReschedule]
        for table in tables:
            session.execute(
                delete(table).where(
                    table.dag_id == self.dag_id,
                    table.task_id == self.task_id,
                    table.run_id == self.run_id,
                    table.map_index == self.map_index,
                )
            )
        for table in tables_by_id:
            session.execute(delete(table).where(table.ti_id == self.id))

    @classmethod
    def duration_expression_update(
        cls, end_date: datetime, query: Update, bind: Engine | SAConnection
    ) -> Update:
        """Return a SQL expression for calculating the duration of this TI, based on the start and end date columns."""
        # TODO: Compare it with self._set_duration method

        if bind.dialect.name == "sqlite":
            return query.values(
                {
                    "end_date": end_date,
                    "duration": (
                        (func.strftime("%s", end_date) - func.strftime("%s", cls.start_date))
                        + func.round((func.strftime("%f", end_date) - func.strftime("%f", cls.start_date)), 3)
                    ),
                }
            )
        elif bind.dialect.name == "postgresql":
            return query.values(
                {
                    "end_date": end_date,
                    "duration": extract("EPOCH", end_date - cls.start_date),
                }
            )

        return query.values(
            {
                "end_date": end_date,
                "duration": (
                    func.timestampdiff(text("MICROSECOND"), cls.start_date, end_date)
                    # Turn microseconds into floating point seconds.
                    / 1_000_000
                ),
            }
        )

    @staticmethod
    def validate_inlet_outlet_assets_activeness(
        inlets: list[AssetProfile], outlets: list[AssetProfile], session: Session
    ) -> None:
        from airflow.sdk.definitions.asset import AssetUniqueKey

        if not (inlets or outlets):
            return

        all_asset_unique_keys = {
            AssetUniqueKey.from_asset(inlet_or_outlet)  # type: ignore
            for inlet_or_outlet in itertools.chain(inlets, outlets)
        }
        inactive_asset_unique_keys = TaskInstance._get_inactive_asset_unique_keys(
            all_asset_unique_keys, session
        )
        if inactive_asset_unique_keys:
            raise AirflowInactiveAssetInInletOrOutletException(inactive_asset_unique_keys)

    @staticmethod
    def _get_inactive_asset_unique_keys(
        asset_unique_keys: set[AssetUniqueKey], session: Session
    ) -> set[AssetUniqueKey]:
        from airflow.sdk.definitions.asset import AssetUniqueKey

        active_asset_unique_keys = {
            AssetUniqueKey(name, uri)
            for name, uri in session.execute(
                select(AssetActive.name, AssetActive.uri).where(
                    tuple_(AssetActive.name, AssetActive.uri).in_(
                        attrs.astuple(key) for key in asset_unique_keys
                    )
                )
            )
        }
        return asset_unique_keys - active_asset_unique_keys

    def get_first_reschedule_date(self, context: Context) -> datetime | None:
        """Get the first reschedule date for the task instance."""
        # TODO: AIP-72: Remove this after `ti.run` is migrated to use Task SDK
        max_tries: int = self.max_tries or 0

        if TYPE_CHECKING:
            assert isinstance(self.task, BaseOperator)

        retries: int = self.task.retries or 0
        first_try_number = max_tries - retries + 1

        with create_session() as session:
            start_date = session.scalar(
                select(TaskReschedule)
                .where(
                    TaskReschedule.ti_id == str(self.id),
                    TaskReschedule.try_number >= first_try_number,
                )
                .order_by(TaskReschedule.id.asc())
                .with_only_columns(TaskReschedule.start_date)
                .limit(1)
            )
        return start_date


def _find_common_ancestor_mapped_group(node1: Operator, node2: Operator) -> MappedTaskGroup | None:
    """Given two operators, find their innermost common mapped task group."""
    if node1.dag is None or node2.dag is None or node1.dag_id != node2.dag_id:
        return None
    parent_group_ids = {g.group_id for g in node1.iter_mapped_task_groups()}
    common_groups = (g for g in node2.iter_mapped_task_groups() if g.group_id in parent_group_ids)
    return next(common_groups, None)


def _is_further_mapped_inside(operator: Operator, container: TaskGroup) -> bool:
    """Whether given operator is *further* mapped inside a task group."""
    from airflow.sdk.definitions.mappedoperator import MappedOperator
    from airflow.sdk.definitions.taskgroup import MappedTaskGroup

    if isinstance(operator, MappedOperator):
        return True
    task_group = operator.task_group
    while task_group is not None and task_group.group_id != container.group_id:
        if isinstance(task_group, MappedTaskGroup):
            return True
        task_group = task_group.parent_group
    return False


# State of the task instance.
# Stores string version of the task state.
TaskInstanceStateType = tuple[TaskInstanceKey, TaskInstanceState]


class SimpleTaskInstance:
    """
    Simplified Task Instance.

    Used to send data between processes via Queues.
    """

    def __init__(
        self,
        dag_id: str,
        task_id: str,
        run_id: str,
        queued_dttm: datetime | None,
        start_date: datetime | None,
        end_date: datetime | None,
        try_number: int,
        map_index: int,
        state: str,
        executor: str | None,
        executor_config: Any,
        pool: str,
        queue: str,
        key: TaskInstanceKey,
        run_as_user: str | None = None,
        priority_weight: int | None = None,
        parent_context_carrier: dict | None = None,
        context_carrier: dict | None = None,
        span_status: str | None = None,
    ):
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id = run_id
        self.map_index = map_index
        self.queued_dttm = queued_dttm
        self.start_date = start_date
        self.end_date = end_date
        self.try_number = try_number
        self.state = state
        self.executor = executor
        self.executor_config = executor_config
        self.run_as_user = run_as_user
        self.pool = pool
        self.priority_weight = priority_weight
        self.queue = queue
        self.key = key
        self.parent_context_carrier = parent_context_carrier
        self.context_carrier = context_carrier
        self.span_status = span_status

    def __repr__(self) -> str:
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"SimpleTaskInstance({attrs})"

    def __eq__(self, other) -> bool:
        if isinstance(other, self.__class__):
            return self.__dict__ == other.__dict__
        return NotImplemented

    @classmethod
    def from_ti(cls, ti: TaskInstance) -> SimpleTaskInstance:
        return cls(
            dag_id=ti.dag_id,
            task_id=ti.task_id,
            run_id=ti.run_id,
            map_index=ti.map_index,
            queued_dttm=ti.queued_dttm,
            start_date=ti.start_date,
            end_date=ti.end_date,
            try_number=ti.try_number,
            state=ti.state,
            executor=ti.executor,
            executor_config=ti.executor_config,
            pool=ti.pool,
            queue=ti.queue,
            key=ti.key,
            run_as_user=ti.run_as_user if hasattr(ti, "run_as_user") else None,
            priority_weight=ti.priority_weight if hasattr(ti, "priority_weight") else None,
            # Inspect the ti, to check if the 'dag_run' relationship is loaded.
            parent_context_carrier=ti.dag_run.context_carrier
            if "dag_run" not in inspect(ti).unloaded
            else None,
            context_carrier=ti.context_carrier if hasattr(ti, "context_carrier") else None,
            span_status=ti.span_status,
        )


class TaskInstanceNote(Base):
    """For storage of arbitrary notes concerning the task instance."""

    __tablename__ = "task_instance_note"
    ti_id = Column(
        String(36).with_variant(postgresql.UUID(as_uuid=False), "postgresql"),
        primary_key=True,
        nullable=False,
    )
    user_id = Column(String(128), nullable=True)
    content = Column(String(1000).with_variant(Text(1000), "mysql"))
    created_at = Column(UtcDateTime, default=timezone.utcnow, nullable=False)
    updated_at = Column(UtcDateTime, default=timezone.utcnow, onupdate=timezone.utcnow, nullable=False)

    task_instance = relationship("TaskInstance", back_populates="task_instance_note", uselist=False)

    __table_args__ = (
        ForeignKeyConstraint(
            (ti_id,),
            [
                "task_instance.id",
            ],
            name="task_instance_note_ti_fkey",
            ondelete="CASCADE",
        ),
    )

    def __init__(self, content, user_id=None):
        self.content = content
        self.user_id = user_id

    def __repr__(self):
        prefix = f"<{self.__class__.__name__}: {self.task_instance.dag_id}.{self.task_instance.task_id} {self.task_instance.run_id}"
        if self.task_instance.map_index != -1:
            prefix += f" map_index={self.task_instance.map_index}"
        return prefix + f" TI ID: {self.ti_id}>"


STATICA_HACK = True
globals()["kcah_acitats"[::-1].upper()] = False
if STATICA_HACK:  # pragma: no cover
    from airflow.jobs.job import Job

    TaskInstance.queued_by_job = relationship(Job)
