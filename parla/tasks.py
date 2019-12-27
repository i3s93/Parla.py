"""
Parla supports simple task parallelism.

.. testsetup::

    T0 = None
    code = None
    from .cpu import cpu

"""
import abc
import logging
import threading
import inspect
from abc import abstractmethod, ABCMeta
from collections import namedtuple
from contextlib import asynccontextmanager
from numbers import Number
from typing import Awaitable, Collection, Iterable, Optional, Dict, Any, Union, Callable

from parla.device import Device, Architecture, get_architecture
from parla.task_runtime import TaskCompleted, TaskRunning, TaskAwaitTasks, TaskState, DeviceDescriptor, Req, Opt

try:
    from parla import task_runtime
except ImportError as e:
    # Ignore the exception if the stack includes the doc generator
    if all("sphinx" not in f.filename for f in inspect.getouterframes(inspect.currentframe())):
        raise

logger = logging.getLogger(__name__)

__all__ = [
    "TaskID", "TaskSpace", "spawn", "get_current_device", "tasks", "finish", "CompletedTaskSpace",
    "DeviceDescriptor", "Req", "Opt"
]


class TaskID:
    """The identity of a task.

    This combines some ID value with the task object itself. The task
    object is assigned by `spawn`. This can be used in place of the
    task object in most places.

    """

    def __init__(self, name, id):
        """"""
        self._name = name
        self._id = id
        self._task = None

    @property
    def task(self):
        """Get the task object associated with this ID.
        """
        if not self._task:
            raise ValueError("This task has not yet been spawned so it cannot be used.")
        return self._task

    @task.setter
    def task(self, v):
        assert not self._task
        self._task = v

    @property
    def id(self):
        """Get the ID object.
        """
        return self._id

    @property
    def name(self):
        """Get the space name.
        """
        return self._name

    def __str__(self):
        return "TaskID({}, {}, task={})".format(self.name, self.id, self._task)

    def __await__(self):
        return (yield TaskAwaitTasks([self.task], self.task))


class TaskSet(Awaitable, Collection, metaclass=ABCMeta):
    """
    A collection of tasks.
    """

    @property
    @abstractmethod
    def _tasks(self) -> Collection:
        pass

    @property
    def _flat_tasks(self) -> Collection:
        # Compute the flat dependency set (including unwrapping TaskID objects)
        deps = []
        for ds in self._tasks:
            if not isinstance(ds, Iterable):
                ds = (ds,)
            for d in ds:
                if hasattr(d, "task"):
                    d = d.task
                if not isinstance(d, task_runtime.Task):
                    raise TypeError("Dependencies must be TaskIDs or Tasks: " + str(d))
                deps.append(d)
        return tuple(deps)

    def __await__(self):
        return (yield TaskAwaitTasks(self._flat_tasks, None))

    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self):
        return iter(self._tasks)

    def __contains__(self, x) -> bool:
        return x in self._tasks


class tasks(TaskSet):
    """
    An ad-hoc collection of tasks.
    An instance is basically a reified dependency list as would be passed to `spawn`.
    This object is awaitable and will block until all tasks are complete.

    >>> await tasks(T1, T2)
    >>> @spawn(None, tasks(T1, T2)) # Same as @spawn(None, [T1, T2])
    >>> def f():
    >>>     pass
    """

    @property
    def _tasks(self) -> Collection:
        return self.args

    __slots__ = ("args",)

    def __init__(self, *args):
        self.args = args


class TaskSpace(TaskSet):
    """A collection of tasks with IDs.

    A `TaskSpace` can be indexed using any hashable values and any
    number of "dimensions" (indicies). If a dimension is indexed with
    numbers then that dimension can be sliced.

    >>> T = TaskSpace()
    ... for i in range(10):
    ...     @spawn(T[i], [T[0:i-1]])
    ...     def t():
    ...         code

    This will produce a series of tasks where each depends on all previous tasks.

    :note: `TaskSpace` does not support assignment to indicies.
    """

    @property
    def _tasks(self):
        return self._data.values()

    def __init__(self, name=""):
        """Create an empty TaskSpace.
        """
        self._name = name
        self._data = {}

    def __getitem__(self, index):
        """Get the `TaskID` associated with the provided indicies.
        """
        if not isinstance(index, tuple):
            index = (index,)
        ret = []

        def traverse(prefix, index):
            if len(index) > 0:
                i, *rest = index
                if isinstance(i, slice):
                    for v in range(i.start or 0, i.stop, i.step or 1):
                        traverse(prefix + (v,), rest)
                elif isinstance(i, Iterable):
                    for v in i:
                        traverse(prefix + (v,), rest)
                else:
                    traverse(prefix + (i,), rest)
            else:
                ret.append(self._data.setdefault(prefix, TaskID(self._name, prefix)))

        traverse((), index)
        # print(index, ret)
        if len(ret) == 1:
            return ret[0]
        return ret


class CompletedTaskSpace(TaskSet):
    """
    A task space that returns completed tasks instead of unused tasks.
    """

    @property
    def _tasks(self) -> Collection:
        return []

    def __getitem__(self, index):
        return tasks()


class _TaskLocals(threading.local):
    def __init__(self):
        super(_TaskLocals, self).__init__()
        self.task_scopes = []

    @property
    def ctx(self):
        return getattr(self, "_ctx", None)

    @ctx.setter
    def ctx(self, v):
        self._ctx = v

    @property
    def global_tasks(self):
        return getattr(self, "_global_tasks", [])

    @global_tasks.setter
    def global_tasks(self, v):
        self._global_tasks = v


_task_locals = _TaskLocals()


def _task_callback(task, body) -> TaskState:
    """
    A function which forwards to a python function in the appropriate device context.
    """
    try:
        with get_current_device().context():
            body = body
            if inspect.iscoroutinefunction(body):
                logger.debug("Constructing coroutine task: %s", task.taskid)
                body = body()

            if inspect.iscoroutine(body):
                try:
                    in_value_task = getattr(task, "value_task", None)
                    in_value = in_value_task and in_value_task.result
                    logger.debug("Executing coroutine task: %s with input %s from %r", task.taskid, in_value_task, in_value)
                    new_task_info = body.send(in_value)
                    task.value_task = None
                    if not isinstance(new_task_info, TaskAwaitTasks):
                        raise TypeError("Parla coroutine tasks must yield a TaskAwaitTasks")
                    dependencies = new_task_info.dependencies
                    value_task = new_task_info.value_task
                    if value_task:
                        assert isinstance(value_task, task_runtime.Task)
                        task.value_task = value_task
                    return TaskRunning(_task_callback, (body,), dependencies)
                except StopIteration as e:
                    result = None
                    if e.args:
                        (result,) = e.args
                    return TaskCompleted(result)
            else:
                logger.debug("Executing function task: %s", task.taskid)
                result = body()
                return TaskCompleted(result)
    finally:
        logger.debug("Finished: %s", task.taskid)
    assert False


def _make_cell(val):
    """
    Create a new Python closure cell object.

    You should not be using this. I shouldn't be either, but I don't know a way around Python's broken semantics.
    """
    x = val

    def closure():
        return x

    return closure.__closure__[0]


def spawn(taskid: Optional[TaskID] = None, dependencies = (), *,
          placement: Device = None,
          memory: int = None,
          devices: Collection[DeviceDescriptor] = None,
          **architectures):
    """
    Execute the body of the function as a new task. The task may start
    executing immediately, so it may execute in parallel with any
    following code.

    >>> @spawn(T1, [T0]) # Create task with ID T1 and dependency on T0
    ... def t():
    ...     code

    >>> @spawn(T1, [T0], cpu=1)
    ... def t():
    ...     code

    :param taskid: the ID of the task in a `TaskSpace` or None if the task does not have an ID.
    :param dependencies: any numer of dependency arguments which may be `Tasks<Task>`, `TaskIDs<TaskID>`, or iterables of Tasks or TaskIDs.
    :param placement: A specific device or architecture that this task should run on.
    :param memory: The amount of memory this task uses. It is assumed to be evenly divided between the devices used.
    :param **architectures: Specify how many devices from each architecture are required. For example, `cpu=1` or `gpu=4`.
    :param devices: A collection of `DeviceDescriptor` objects. This parameter is only allowed if `placement`,
        `memory`, and `architecture` are not provided. It is the advanced interface.

    The declared task (`t` above) can be used as a dependency for later tasks (in place of the tasks ID).
    This same value is stored into the task space used in `taskid`.

    :see: :ref:`Fox's Algorithm` Example

    """

    def decorator(body):
        nonlocal taskid, devices

        if devices is not None and (
            placement is not None or memory is not None or architectures):
            raise ValueError("If devices is specified, then placement, memory and architectures are not allowed.")


        if placement is not None:
            assert devices is None
            devices = [Req(placement)]
        else:
            devices = list(devices) if devices is not None else []

        for name, total_amount in architectures.items():
            while total_amount:
                if total_amount > 1:
                    amount = 1
                else:
                    amount = total_amount
                devices.append(Req(get_architecture(name), devices=amount))
                total_amount -= amount

        if not devices:
            devices.append(Req())

        if inspect.isgeneratorfunction(body):
            raise TypeError("Spawned tasks must be normal functions or coroutines; not generators.")

        # Compute the flat dependency set (including unwrapping TaskID objects)
        deps = []
        for ds in dependencies:
            if not isinstance(ds, Iterable):
                ds = (ds,)
            for d in ds:
                if hasattr(d, "task"):
                    d = d.task
                if not isinstance(d, task_runtime.Task):
                    raise TypeError("Dependencies must be TaskIDs or Tasks: " + str(d))
                deps.append(d)

        if inspect.iscoroutine(body):
            # An already running coroutine does not need changes since we assume
            # it was changed correctly when the original function was spawned.
            separated_body = body
        else:
            # Perform a horrifying hack to build a new function which will
            # not be able to observe changes in the original cells in the
            # tasks outer scope. To do this we build a new function with a
            # replaced closure which contains new cells.
            separated_body = type(body)(
                body.__code__, body.__globals__, body.__name__, body.__defaults__,
                closure=body.__closure__ and tuple(_make_cell(x.cell_contents) for x in body.__closure__))
            separated_body.__annotations__ = body.__annotations__
            separated_body.__doc__ = body.__doc__
            separated_body.__kwdefaults__ = body.__kwdefaults__
            separated_body.__module__ = body.__module__

        if not taskid:
            taskid = TaskID("global_" + str(len(_task_locals.global_tasks)), len(_task_locals.global_tasks))
            _task_locals.global_tasks += [taskid]
        taskid.dependencies = dependencies

        # Spawn the task via the Parla runtime API
        task = task_runtime.get_scheduler_context().spawn_task(
            _task_callback, (separated_body,), deps, taskid=taskid, devices=devices)

        logger.debug("Created: %s %r", taskid, body)

        for scope in _task_locals.task_scopes:
            scope.append(task)

        # Return the task object
        return task

    return decorator


def get_current_device() -> Device:
    return task_runtime.get_device()


@asynccontextmanager
async def finish():
    """
    Execute the body of the `with` normally and then perform a barrier applying to all tasks created within this block
    and in this task.

    `finish` does not wait for tasks which are created by the tasks it waits on. This is because tasks are allowed to
    complete before tasks they create. This is a difference from Cilk.

    >>> async with finish():
    ...     @spawn()
    ...     def task():
    ...         @spawn()
    ...         def subtask():
    ...              code
    ...         code
    ... # After the finish block, task will be complete, but subtask may not be.

    """
    my_tasks = []
    _task_locals.task_scopes.append(my_tasks)
    try:
        yield
    finally:
        removed_tasks = _task_locals.task_scopes.pop()
        assert removed_tasks is my_tasks
        await tasks(my_tasks)
