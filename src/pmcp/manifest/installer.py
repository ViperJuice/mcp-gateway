"""Server installer - platform-specific MCP server installation with background jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pmcp.manifest.environment import Platform
from pmcp.manifest.loader import ServerConfig

logger = logging.getLogger(__name__)

# Heartbeat timeout - kill if no output for this long
HEARTBEAT_TIMEOUT = 120  # seconds

# Time to wait before assuming uvx server is ready (they don't output startup messages)
UVX_STARTUP_SECONDS = 3


class InstallError(Exception):
    """Error during server installation."""

    pass


class MissingApiKeyError(Exception):
    """API key required but not set."""

    def __init__(self, env_var: str, env_instructions: str, env_path: Path):
        self.env_var = env_var
        self.env_instructions = env_instructions
        self.env_path = env_path
        super().__init__(f"Missing required environment variable: {env_var}")


@dataclass
class InstallJob:
    """Tracks a running installation job."""

    id: str
    server_name: str
    status: Literal[
        "pending", "installing", "server_ready", "complete", "failed", "timeout"
    ] = "pending"
    progress: int = 0  # 0-100
    output_lines: list[str] = field(default_factory=list)
    last_heartbeat: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    process: asyncio.subprocess.Process | None = None
    error: str | None = None
    command: str = ""  # The command used (e.g., "uvx", "npx")
    _monitor_task: asyncio.Task | None = field(default=None, repr=False)


class JobManager:
    """Manages background installation jobs."""

    _instance: JobManager | None = None
    _jobs: dict[str, InstallJob]

    def __new__(cls) -> JobManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._jobs = {}
        return cls._instance

    @classmethod
    def get_instance(cls) -> JobManager:
        """Get the singleton instance."""
        return cls()

    def get_job(self, job_id: str) -> InstallJob | None:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> list[InstallJob]:
        """Get all jobs."""
        return list(self._jobs.values())

    async def start_install(
        self,
        server_config: ServerConfig,
        platform: Platform,
    ) -> str:
        """Start a background installation job.

        Returns:
            Job ID for tracking progress
        """
        # Check API key first
        await check_api_key(server_config)

        # Get platform-specific install command
        install_cmd = server_config.install.get(platform)
        if not install_cmd:
            if platform == "wsl":
                install_cmd = server_config.install.get("linux")

        if not install_cmd:
            raise InstallError(
                f"No install command for {server_config.name} on {platform}"
            )

        # Create job
        job_id = str(uuid.uuid4())[:8]
        job = InstallJob(
            id=job_id,
            server_name=server_config.name,
            status="pending",
            command=install_cmd[0] if install_cmd else "",
        )
        self._jobs[job_id] = job

        logger.info(
            f"Starting install job {job_id} for {server_config.name}: {' '.join(install_cmd)}"
        )

        try:
            # Start subprocess with stdin/stdout/stderr pipes
            # stdin is needed for JSON-RPC communication after server starts
            # stderr is separate so ClientManager can read it independently
            process = await asyncio.create_subprocess_exec(
                *install_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            job.process = process
            job.status = "installing"
            job.last_heartbeat = time.time()

            # Start monitoring task with exception isolation
            job._monitor_task = asyncio.create_task(
                self._monitor_install(job),
                name=f"install-monitor-{job_id}",
            )
            # Add callback to handle task exceptions without crashing gateway
            job._monitor_task.add_done_callback(
                lambda t: self._handle_task_exception(t, job)
            )

        except FileNotFoundError as e:
            job.status = "failed"
            job.error = f"Command not found: {e}"
            logger.error(f"Install job {job_id} failed: {job.error}")

        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            logger.error(f"Install job {job_id} failed: {job.error}")

        return job_id

    def _handle_task_exception(self, task: asyncio.Task, job: InstallJob) -> None:
        """Handle exceptions from monitor task without crashing gateway."""
        try:
            exc = task.exception()
            if exc:
                logger.error(f"Install job {job.id} task crashed: {exc}")
                if job.status == "installing":
                    job.status = "failed"
                    job.error = f"Monitor task crashed: {exc}"
                # Kill subprocess if still running (but NOT if server_ready - it's being handed off)
                if (
                    job.status != "server_ready"
                    and job.process
                    and job.process.returncode is None
                ):
                    try:
                        job.process.kill()
                    except Exception:
                        pass
        except asyncio.CancelledError:
            # Task was cancelled, not an error
            pass
        except asyncio.InvalidStateError:
            # Task not done yet, shouldn't happen in done callback
            pass

    async def _monitor_install(self, job: InstallJob) -> None:
        """Monitor subprocess output as heartbeat, kill on timeout.

        Reads from both stdout and stderr - npx writes progress to stderr,
        but the MCP server writes startup message to stdout.

        This method is fully isolated - all exceptions are caught and logged,
        never propagated to crash the gateway.
        """
        process = job.process
        if not process or not process.stdout:
            job.status = "failed"
            job.error = "No process to monitor"
            return

        # Detect uvx-based servers (they don't output startup messages)
        is_uvx = "uvx" in job.command.lower()
        start_time = time.time()

        # Create tasks to read from both streams
        async def read_line(
            stream: asyncio.StreamReader, name: str
        ) -> tuple[str, bytes | None]:
            """Read a line from a stream, return (stream_name, line)."""
            try:
                line = await stream.readline()
                return (name, line)
            except Exception:
                return (name, None)

        try:
            # Track active read tasks
            stdout_task: asyncio.Task | None = None
            stderr_task: asyncio.Task | None = None

            while True:
                # Check if process has exited
                if process.returncode is not None:
                    break

                # Create read tasks if not already pending
                if stdout_task is None and process.stdout:
                    stdout_task = asyncio.create_task(
                        read_line(process.stdout, "stdout")
                    )
                if stderr_task is None and process.stderr:
                    stderr_task = asyncio.create_task(
                        read_line(process.stderr, "stderr")
                    )

                # Wait for either stream to have output (or timeout)
                pending_tasks = [t for t in [stdout_task, stderr_task] if t is not None]
                if not pending_tasks:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    # Use shorter timeout for uvx to check readiness more frequently
                    poll_timeout = 1.0 if is_uvx else HEARTBEAT_TIMEOUT

                    done, pending = await asyncio.wait(
                        pending_tasks,
                        timeout=poll_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if not done:
                        # Timeout - no output from either stream
                        elapsed_since_heartbeat = time.time() - job.last_heartbeat
                        elapsed_since_start = time.time() - start_time

                        # For uvx servers: ready after process runs stable for UVX_STARTUP_SECONDS
                        if is_uvx and elapsed_since_start >= UVX_STARTUP_SECONDS:
                            if process.returncode is None:
                                logger.info(
                                    f"Install {job.id}: uvx server ready (process stable for {elapsed_since_start:.1f}s)"
                                )
                                job.status = "server_ready"
                                job.progress = 100
                                # Cancel pending tasks
                                for task in pending:
                                    task.cancel()
                                return
                            # Process exited - will be handled below

                        # For uvx, keep polling until UVX_STARTUP_SECONDS
                        if is_uvx and elapsed_since_start < UVX_STARTUP_SECONDS:
                            continue

                        # Regular heartbeat timeout check
                        if elapsed_since_heartbeat >= HEARTBEAT_TIMEOUT:
                            logger.warning(
                                f"Install job {job.id} stalled (no output for {elapsed_since_heartbeat:.0f}s), killing"
                            )
                            # Cancel pending tasks
                            for task in pending:
                                task.cancel()
                            await self._safe_terminate_process(
                                process, job.id, force=True
                            )
                            job.status = "timeout"
                            job.error = f"Installation stalled (no output for {HEARTBEAT_TIMEOUT}s)"
                            return

                    # Process completed tasks
                    for task in done:
                        stream_name, line = task.result()

                        # Clear the task so we create a new one next iteration
                        if stream_name == "stdout":
                            stdout_task = None
                        else:
                            stderr_task = None

                        if line:
                            # Got output - update heartbeat
                            job.last_heartbeat = time.time()
                            try:
                                decoded = line.decode().strip()
                            except Exception:
                                decoded = str(line)

                            if decoded:
                                job.output_lines.append(f"[{stream_name}] {decoded}")
                                # Keep last 20 lines
                                job.output_lines = job.output_lines[-20:]
                                # Try to parse progress from npm output
                                try:
                                    job.progress = self._parse_progress(
                                        decoded, job.progress
                                    )
                                except Exception:
                                    pass
                                logger.debug(
                                    f"Install {job.id} [{stream_name}]: {decoded}"
                                )

                                # Check both stdout AND stderr for server started
                                # (some servers like memory-mcp write startup to stderr)
                                try:
                                    if self._is_server_started(decoded):
                                        logger.info(
                                            f"Install {job.id}: Server started on {stream_name}, ready for handoff"
                                        )
                                        job.status = "server_ready"
                                        job.progress = 100
                                        # Cancel pending tasks
                                        for t in pending:
                                            t.cancel()
                                        # DON'T terminate - process stays running for ClientManager
                                        return
                                except Exception as e:
                                    logger.warning(
                                        f"Install {job.id}: Error in server detection: {e}"
                                    )

                except asyncio.CancelledError:
                    # Task was cancelled - clean up
                    if stdout_task:
                        stdout_task.cancel()
                    if stderr_task:
                        stderr_task.cancel()
                    raise

            # Process finished - check return code
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"Install {job.id}: Process didn't exit cleanly")
                await self._safe_terminate_process(process, job.id, force=True)

            if process.returncode == 0:
                job.status = "complete"
                job.progress = 100
                logger.info(f"Install job {job.id} completed successfully")
            else:
                job.status = "failed"
                job.error = f"Process exited with code {process.returncode}"
                if job.output_lines:
                    job.error += f": {job.output_lines[-1]}"
                logger.error(f"Install job {job.id} failed: {job.error}")

        except asyncio.CancelledError:
            # Task was cancelled - clean up
            logger.info(f"Install job {job.id} monitor cancelled")
            await self._safe_terminate_process(process, job.id, force=True)
            job.status = "failed"
            job.error = "Installation cancelled"

        except Exception as e:
            logger.error(f"Install job {job.id} monitor error: {e}", exc_info=True)
            job.status = "failed"
            job.error = str(e)
            # Try to clean up process
            await self._safe_terminate_process(process, job.id, force=True)

        finally:
            # Only clear process if NOT server_ready (process needed for handoff)
            if job.status != "server_ready":
                job.process = None  # Allow GC

    async def _safe_terminate_process(
        self, process: asyncio.subprocess.Process, job_id: str, force: bool = False
    ) -> None:
        """Safely terminate a subprocess with timeout and fallback to kill."""
        try:
            if process.returncode is not None:
                return  # Already exited

            if force:
                process.kill()
            else:
                process.terminate()

            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                if not force:
                    logger.warning(
                        f"Install {job_id}: Process didn't terminate, killing"
                    )
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        logger.error(f"Install {job_id}: Process won't die!")
        except Exception as e:
            logger.warning(f"Install {job_id}: Error terminating process: {e}")

    def _parse_progress(self, line: str, current: int) -> int:
        """Try to parse progress percentage from output line."""
        # npm shows progress like "⸨███████████████████░░░░░░░░░░░░░░░░░░░░░⸩ 45%"
        # or "reify:lodash: timing reifyNode:node_modules/lodash Completed in 234ms"
        import re

        # Look for percentage
        match = re.search(r"(\d{1,3})%", line)
        if match:
            return min(int(match.group(1)), 99)  # Cap at 99 until complete

        # Increment slightly for activity
        if current < 90:
            return current + 1

        return current

    def _is_server_started(self, line: str) -> bool:
        """Detect if an MCP server has started running.

        For npx-based packages, the install command also runs the server.
        We detect common startup patterns to know when to stop monitoring.
        """
        line_lower = line.lower()

        # Common MCP server startup messages
        startup_patterns = [
            "running on stdio",
            "server running",
            "server started",
            "listening on stdio",
            "mcp server ready",
            "ready to accept connections",
            "waiting for connection",
            "initialized",  # Many servers log this when ready
        ]

        for pattern in startup_patterns:
            if pattern in line_lower:
                return True

        return False

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a running installation job."""
        job = self._jobs.get(job_id)
        if not job:
            return False

        if job.process and job.process.returncode is None:
            logger.info(f"Cancelling install job {job_id}")
            job.process.terminate()

            # Give it a moment to terminate gracefully
            try:
                await asyncio.wait_for(job.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                job.process.kill()
                await job.process.wait()

            job.status = "failed"
            job.error = "Cancelled by user"

        if job._monitor_task:
            job._monitor_task.cancel()

        return True

    def cleanup_old_jobs(self, max_age: float = 3600) -> int:
        """Remove jobs older than max_age seconds."""
        now = time.time()
        old_jobs = [
            job_id
            for job_id, job in self._jobs.items()
            if now - job.started_at > max_age
            and job.status in ("complete", "failed", "timeout")
        ]
        for job_id in old_jobs:
            del self._jobs[job_id]
        return len(old_jobs)


# Module-level singleton accessor
def get_job_manager() -> JobManager:
    """Get the global job manager instance."""
    return JobManager.get_instance()


async def check_api_key(server_config: ServerConfig) -> None:
    """Check if required API key is set.

    Raises:
        MissingApiKeyError: If API key is required but not set
    """
    if not server_config.requires_api_key:
        return

    env_var = server_config.env_var
    if not env_var:
        return

    if not os.environ.get(env_var):
        env_path = Path.cwd() / ".env"
        raise MissingApiKeyError(
            env_var=env_var,
            env_instructions=server_config.env_instructions or f"Set {env_var} in .env",
            env_path=env_path,
        )


async def install_server(
    server_config: ServerConfig,
    platform: Platform,
    timeout: float = 120.0,
) -> None:
    """Install an MCP server using platform-specific commands (blocking).

    This is the legacy synchronous interface. For background installation
    with progress tracking, use JobManager.start_install() instead.

    Args:
        server_config: Server configuration from manifest
        platform: Current platform (mac, wsl, linux, windows)
        timeout: Installation timeout in seconds

    Raises:
        InstallError: If installation fails
        MissingApiKeyError: If API key is required but not set
    """
    # Check API key first
    await check_api_key(server_config)

    # Get platform-specific install command
    install_cmd = server_config.install.get(platform)
    if not install_cmd:
        # Try linux as fallback for wsl
        if platform == "wsl":
            install_cmd = server_config.install.get("linux")

    if not install_cmd:
        raise InstallError(f"No install command for {server_config.name} on {platform}")

    logger.info(f"Installing {server_config.name}: {' '.join(install_cmd)}")

    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *install_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )

        if process.returncode != 0:
            error_output = stderr.decode() or stdout.decode()
            raise InstallError(
                f"Installation of {server_config.name} failed: {error_output[:500]}"
            )

        logger.info(f"Successfully installed {server_config.name}")

    except asyncio.TimeoutError:
        # Kill the process on timeout
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise InstallError(
            f"Installation of {server_config.name} timed out after {timeout}s"
        )
    except FileNotFoundError as e:
        raise InstallError(f"Command not found for {server_config.name}: {e}")


async def verify_installation(server_config: ServerConfig) -> bool:
    """Verify that a server is installed and runnable.

    Returns:
        True if server can be started, False otherwise
    """
    try:
        # Try to start the server briefly
        process = await asyncio.create_subprocess_exec(
            server_config.command,
            *server_config.args[:1],  # Just first arg to test
            "--help",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        await asyncio.wait_for(process.communicate(), timeout=5.0)
        return True

    except Exception:
        return False
