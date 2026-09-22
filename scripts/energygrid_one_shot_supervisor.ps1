# Energy@Grid one-shot supervisor.
#
# This file is deliberately a standalone Windows PowerShell 5.1 boundary.  It owns
# containment, timing and bounded evidence only; the installed launcher remains the
# authority for credentials, configuration and EnergyGrid business behaviour.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$LauncherPath,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ExpectedLauncherSha256,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ExpectedLauncherLibrarySha256,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ConfigPath,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$PythonExe,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$CheckoutRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$CredentialPath,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$BrowserCachePath,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ExpectedBranch,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string[]]$AuthorisedLauncherRootWriteSid,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$LogRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$EvidenceRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$RunId,
    [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Dot-sourcing would make the supervisor's exit and handle-ownership boundary ambiguous.
if ($MyInvocation.InvocationName -eq '.') {
    throw 'EG_SUPERVISOR_DOTSOURCING_REJECTED'
}

$script:EgState = [pscustomobject]@{
    support_ref = 'EG_SUPERVISOR'
    error_code = $null
    creation_attempted = $false
    creation_succeeded = $false
    resume_attempted = $false
    resume_succeeded = $false
    intent_committed = $false
    outcome_committed = $false
    intent_bytes = $null
    launcher_exit_code = $null
    application_child_observed = $false
    application_child_observation_elapsed_ms = $null
    observer_failed = $false
    total_processes = $null
    active_processes = $null
    total_terminated_processes = $null
    baseline_total_processes = $null
    baseline_active_processes = $null
    reap_confirmed = $false
    stdout_bytes = [uint64]0
    stderr_bytes = [uint64]0
    stdout_complete = $false
    stderr_complete = $false
    timed_out = $false
    interrupted = $false
    termination_started = $false
    termination_succeeded = $false
    termination_failure = $false
    containment_failure = $false
    evidence_integrity_failure = $false
    drain_failure = $false
    descendant_grace_expired = $false
    start_verdict = 'AMBIGUOUS'
    outcome_write_attempted = $false
    duplicate_run_id = $false
    precreate_rejection = $false
}

$script:EgNativeSource = @'
using System;
using System.IO;
using System.Management;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

public static class EnergyGridOneShotSupervisorNative
{
    public const uint PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D;
    public const uint PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002;
    public const uint EXTENDED_STARTUPINFO_PRESENT = 0x00080000;
    public const uint CREATE_SUSPENDED = 0x00000004;
    public const uint CREATE_NO_WINDOW = 0x08000000;
    public const uint STARTF_USESTDHANDLES = 0x00000100;
    public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    public const uint SUPERVISOR_TERMINATION_EXIT_CODE = 0xE0470001;
    public const uint HANDLE_FLAG_INHERIT = 0x00000001;
    public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000;
    public const uint SYNCHRONIZE = 0x00100000;
    public const uint WAIT_OBJECT_0 = 0;
    public const uint WAIT_TIMEOUT = 258;
    public const uint WAIT_FAILED = 0xFFFFFFFF;
    public const int ERROR_INSUFFICIENT_BUFFER = 122;
    public const int ERROR_MORE_DATA = 234;
    public const int JobObjectBasicAccountingInformation = 1;
    public const int JobObjectBasicProcessIdList = 3;
    public const int JobObjectExtendedLimitInformation = 9;
    public const uint STILL_ACTIVE = 259;

    [StructLayout(LayoutKind.Sequential)]
    public struct SECURITY_ATTRIBUTES
    {
        public int nLength;
        public IntPtr lpSecurityDescriptor;
        public int bInheritHandle;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct STARTUPINFO
    {
        public uint cb;
        public IntPtr lpReserved;
        public IntPtr lpDesktop;
        public IntPtr lpTitle;
        public uint dwX;
        public uint dwY;
        public uint dwXSize;
        public uint dwYSize;
        public uint dwXCountChars;
        public uint dwYCountChars;
        public uint dwFillAttribute;
        public uint dwFlags;
        public ushort wShowWindow;
        public ushort cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct STARTUPINFOEX
    {
        public STARTUPINFO StartupInfo;
        public IntPtr lpAttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION
    {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS
    {
        public long ReadOperationCount;
        public long WriteOperationCount;
        public long OtherOperationCount;
        public long ReadTransferCount;
        public long WriteTransferCount;
        public long OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    {
        public long TotalUserTime;
        public long TotalKernelTime;
        public long ThisPeriodTotalUserTime;
        public long ThisPeriodTotalKernelTime;
        public uint TotalPageFaultCount;
        public uint TotalProcesses;
        public uint ActiveProcesses;
        public uint TotalTerminatedProcesses;
    }

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate bool HandlerRoutine(uint signal);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    public static extern IntPtr CreateJobObjectW(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetInformationJobObject(
        IntPtr hJob, int jobObjectInfoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info,
        uint infoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool QueryInformationJobObject(
        IntPtr hJob, int jobObjectInfoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info,
        uint infoLength, out uint returnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool QueryInformationJobObject(
        IntPtr hJob, int jobObjectInfoClass, ref JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info,
        uint infoLength, out uint returnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool QueryInformationJobObject(
        IntPtr hJob, int jobObjectInfoClass, IntPtr info, uint infoLength, out uint returnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool InitializeProcThreadAttributeList(
        IntPtr attributeList, int attributeCount, uint flags, ref IntPtr size);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool UpdateProcThreadAttribute(
        IntPtr attributeList, uint flags, IntPtr attribute, IntPtr value, IntPtr size,
        IntPtr previousValue, IntPtr returnSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern void DeleteProcThreadAttributeList(IntPtr attributeList);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    public static extern bool CreateProcessW(
        string applicationName, StringBuilder commandLine, IntPtr processAttributes,
        IntPtr threadAttributes, bool inheritHandles, uint creationFlags, IntPtr environment,
        string currentDirectory, ref STARTUPINFOEX startupInfo, out PROCESS_INFORMATION processInfo);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool IsProcessInJob(IntPtr processHandle, IntPtr jobHandle, out bool result);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint ResumeThread(IntPtr threadHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool TerminateJobObject(IntPtr jobHandle, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetExitCodeProcess(IntPtr processHandle, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CreatePipe(
        out IntPtr readPipe, out IntPtr writePipe, ref SECURITY_ATTRIBUTES attributes, uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, uint processId);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool QueryFullProcessImageNameW(
        IntPtr processHandle, uint flags, StringBuilder imagePath, ref uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleCtrlHandler(HandlerRoutine handler, bool add);

    public sealed class NativeFailureException : Exception
    {
        public int ErrorCode { get; private set; }
        public NativeFailureException(int errorCode) : base("native_failure")
        {
            ErrorCode = errorCode;
        }
    }

    public sealed class NativeBooleanResult
    {
        public bool Success;
        public int ErrorCode;
    }

    public sealed class MembershipResult
    {
        public bool Succeeded;
        public bool IsMember;
        public int ErrorCode;
    }

    public sealed class JobAccountingResult
    {
        public bool Succeeded;
        public int ErrorCode;
        public ulong TotalProcesses;
        public ulong ActiveProcesses;
        public ulong TotalTerminatedProcesses;
    }

    public sealed class ProcessIdsResult
    {
        public bool Succeeded;
        public int ErrorCode;
        public uint[] ProcessIds = new uint[0];
    }

    public sealed class JobLimitsResult
    {
        public bool Succeeded;
        public bool Matches;
        public int ErrorCode;
    }

    public sealed class ProcessHandleResult
    {
        public bool Succeeded;
        public IntPtr Handle;
        public int ErrorCode;
    }

    public sealed class ProcessLiveResult
    {
        public bool Succeeded;
        public bool Live;
        public uint ExitCode;
        public int ErrorCode;
    }

    public sealed class ImageResult
    {
        public bool Succeeded;
        public string ImagePath = String.Empty;
        public int ErrorCode;
    }

    public sealed class ProcessCreationResult
    {
        public bool Succeeded;
        public PROCESS_INFORMATION ProcessInfo;
        public int ErrorCode;
    }

    public sealed class ResumeResult
    {
        public bool Attempted;
        public bool Accepted;
        public bool DeadlineExpired;
        public uint ReturnValue;
        public int ErrorCode;
    }

    public sealed class AttributeResources : IDisposable
    {
        public IntPtr AttributeList { get; private set; }
        private IntPtr buffer;
        private IntPtr jobValue;
        private IntPtr handleValue;
        private bool initialized;
        private bool disposed;

        public AttributeResources(IntPtr job, IntPtr stdin, IntPtr stdout, IntPtr stderr)
        {
            try
            {
                IntPtr required = IntPtr.Zero;
                bool first = InitializeProcThreadAttributeList(
                    IntPtr.Zero, 2, 0, ref required);
                int firstError = Marshal.GetLastWin32Error();
                if (first || firstError != ERROR_INSUFFICIENT_BUFFER || required == IntPtr.Zero)
                {
                    throw new NativeFailureException(firstError);
                }

                buffer = Marshal.AllocHGlobal(required);
                AttributeList = buffer;
                bool second = InitializeProcThreadAttributeList(
                    AttributeList, 2, 0, ref required);
                int secondError = Marshal.GetLastWin32Error();
                if (!second)
                {
                    throw new NativeFailureException(secondError);
                }
                initialized = true;

                jobValue = Marshal.AllocHGlobal(IntPtr.Size);
                Marshal.WriteIntPtr(jobValue, job);
                bool jobUpdated = UpdateProcThreadAttribute(
                    AttributeList, 0, (IntPtr)PROC_THREAD_ATTRIBUTE_JOB_LIST,
                    jobValue, (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero);
                int jobError = Marshal.GetLastWin32Error();
                if (!jobUpdated)
                {
                    throw new NativeFailureException(jobError);
                }

                handleValue = Marshal.AllocHGlobal(IntPtr.Size * 3);
                Marshal.WriteIntPtr(handleValue, 0, stdin);
                Marshal.WriteIntPtr(handleValue, IntPtr.Size, stdout);
                Marshal.WriteIntPtr(handleValue, IntPtr.Size * 2, stderr);
                bool handlesUpdated = UpdateProcThreadAttribute(
                    AttributeList, 0, (IntPtr)PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    handleValue, (IntPtr)(IntPtr.Size * 3), IntPtr.Zero, IntPtr.Zero);
                int handlesError = Marshal.GetLastWin32Error();
                if (!handlesUpdated)
                {
                    throw new NativeFailureException(handlesError);
                }
            }
            catch
            {
                Dispose();
                throw;
            }
        }

        public void Dispose()
        {
            if (disposed)
            {
                return;
            }
            disposed = true;
            if (initialized)
            {
                DeleteProcThreadAttributeList(AttributeList);
                initialized = false;
            }
            if (handleValue != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(handleValue);
                handleValue = IntPtr.Zero;
            }
            if (jobValue != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(jobValue);
                jobValue = IntPtr.Zero;
            }
            if (buffer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(buffer);
                buffer = IntPtr.Zero;
                AttributeList = IntPtr.Zero;
            }
        }
    }

    public sealed class PipeSet
    {
        public IntPtr LauncherStdinRead;
        public IntPtr SupervisorStdinWrite;
        public IntPtr SupervisorStdoutRead;
        public IntPtr LauncherStdoutWrite;
        public IntPtr SupervisorStderrRead;
        public IntPtr LauncherStderrWrite;
    }

    public sealed class DrainWorker
    {
        private readonly IntPtr sourceHandle;
        private readonly Thread thread;
        private int completed;
        private int failed;
        private ulong bytes;

        public DrainWorker(IntPtr handle)
        {
            sourceHandle = handle;
            thread = new Thread(Drain);
            thread.IsBackground = true;
            thread.Start();
        }

        public ulong Bytes { get { return bytes; } }
        public bool Completed { get { return Volatile.Read(ref completed) != 0; } }
        public bool Failed { get { return Volatile.Read(ref failed) != 0; } }

        private void Drain()
        {
            byte[] buffer = new byte[65536];
            try
            {
                using (SafeFileHandle safe = new SafeFileHandle(sourceHandle, true))
                using (FileStream stream = new FileStream(safe, FileAccess.Read, 65536, false))
                {
                    while (true)
                    {
                        int read = stream.Read(buffer, 0, buffer.Length);
                        if (read == 0)
                        {
                            break;
                        }
                        bytes = checked(bytes + (ulong)read);
                        Array.Clear(buffer, 0, buffer.Length);
                    }
                }
            }
            catch
            {
                Interlocked.Exchange(ref failed, 1);
            }
            finally
            {
                Array.Clear(buffer, 0, buffer.Length);
                Interlocked.Exchange(ref completed, 1);
            }
        }

        public bool Join(int milliseconds)
        {
            return thread.Join(milliseconds);
        }
    }

    public sealed class ProcessMetadataResult
    {
        public bool Succeeded;
        public bool TimedOut;
        public bool ProviderFailed;
        public uint ParentProcessId;
        public string CommandLine = String.Empty;
    }

    public sealed class DurabilityResult
    {
        public bool Succeeded;
        public bool TimedOut;
        public int ErrorCode;
    }

    public sealed class WaitResult
    {
        public uint Value;
        public int ErrorCode;
    }

    private static readonly object controlLock = new object();
    private static int terminationRequested;
    private static int failureRequested;
    private static int resumeAttempted;
    private static int resumeGateClosed;
    private static int intentCommitted;
    private static int consoleHandlerInstalled;
    private static int observerBusy;
    private static readonly HandlerRoutine consoleHandler = HandleConsoleSignal;

    private static bool HandleConsoleSignal(uint signal)
    {
        if (signal == 0 || signal == 1 || signal == 2 || signal == 5 || signal == 6)
        {
            Interlocked.Exchange(ref terminationRequested, 1);
            return true;
        }
        return false;
    }

    public static void ResetControlState()
    {
        lock (controlLock)
        {
            terminationRequested = 0;
            failureRequested = 0;
            resumeAttempted = 0;
            resumeGateClosed = 0;
            intentCommitted = 0;
        }
    }

    public static bool IsTerminationRequested
    {
        get { return Volatile.Read(ref terminationRequested) != 0; }
    }

    public static bool IsFailureRequested
    {
        get { return Volatile.Read(ref failureRequested) != 0; }
    }

    public static bool InstallConsoleHandler(out int errorCode)
    {
        if (SetConsoleCtrlHandler(consoleHandler, true))
        {
            Interlocked.Exchange(ref consoleHandlerInstalled, 1);
            errorCode = 0;
            return true;
        }
        errorCode = Marshal.GetLastWin32Error();
        return false;
    }

    public static bool UninstallConsoleHandler(out int errorCode)
    {
        if (Volatile.Read(ref consoleHandlerInstalled) == 0)
        {
            errorCode = 0;
            return true;
        }
        if (SetConsoleCtrlHandler(consoleHandler, false))
        {
            Interlocked.Exchange(ref consoleHandlerInstalled, 0);
            errorCode = 0;
            return true;
        }
        errorCode = Marshal.GetLastWin32Error();
        return false;
    }

    public static void RequestFailure()
    {
        lock (controlLock)
        {
            failureRequested = 1;
            resumeGateClosed = 1;
        }
    }

    public static bool CommitIntent()
    {
        lock (controlLock)
        {
            if (failureRequested != 0 || terminationRequested != 0 || resumeGateClosed != 0)
            {
                return false;
            }
            intentCommitted = 1;
            return true;
        }
    }

    public static ResumeResult TryResumeThread(IntPtr threadHandle, long deadlineTicks)
    {
        lock (controlLock)
        {
            ResumeResult result = new ResumeResult();
            if (resumeAttempted != 0 || intentCommitted == 0 || failureRequested != 0 ||
                terminationRequested != 0 || resumeGateClosed != 0)
            {
                result.Attempted = false;
                result.Accepted = false;
                result.ReturnValue = UInt32.MaxValue;
                result.ErrorCode = 0;
                return result;
            }

            if (System.Diagnostics.Stopwatch.GetTimestamp() >= deadlineTicks)
            {
                resumeGateClosed = 1;
                failureRequested = 1;
                result.Attempted = false;
                result.Accepted = false;
                result.DeadlineExpired = true;
                result.ReturnValue = UInt32.MaxValue;
                result.ErrorCode = 0;
                return result;
            }

            resumeAttempted = 1;
            resumeGateClosed = 1;
            uint value = ResumeThread(threadHandle);
            int error = value == UInt32.MaxValue ? Marshal.GetLastWin32Error() : 0;
            result.Attempted = true;
            result.Accepted = value == 1;
            result.ReturnValue = value;
            result.ErrorCode = error;
            if (!result.Accepted)
            {
                failureRequested = 1;
            }
            return result;
        }
    }

    public static NativeBooleanResult CloseHandleChecked(IntPtr handle)
    {
        NativeBooleanResult result = new NativeBooleanResult();
        if (handle == IntPtr.Zero)
        {
            result.Success = true;
            result.ErrorCode = 0;
            return result;
        }
        bool success = CloseHandle(handle);
        result.Success = success;
        result.ErrorCode = success ? 0 : Marshal.GetLastWin32Error();
        return result;
    }

    public static NativeBooleanResult SetHandleInheritance(IntPtr handle, bool inherit)
    {
        NativeBooleanResult result = new NativeBooleanResult();
        bool success = SetHandleInformation(
            handle, HANDLE_FLAG_INHERIT, inherit ? HANDLE_FLAG_INHERIT : 0);
        result.Success = success;
        result.ErrorCode = success ? 0 : Marshal.GetLastWin32Error();
        return result;
    }

    public static PipeSet CreatePipes()
    {
        SECURITY_ATTRIBUTES attributes = new SECURITY_ATTRIBUTES();
        attributes.nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        attributes.bInheritHandle = 1;
        PipeSet pipes = new PipeSet();
        try
        {
            if (!CreatePipe(out pipes.LauncherStdinRead, out pipes.SupervisorStdinWrite,
                ref attributes, 0))
            {
                int error = Marshal.GetLastWin32Error();
                throw new NativeFailureException(error);
            }
            NativeBooleanResult stdinParent = SetHandleInheritance(
                pipes.SupervisorStdinWrite, false);
            if (!stdinParent.Success)
            {
                throw new NativeFailureException(stdinParent.ErrorCode);
            }

            if (!CreatePipe(out pipes.SupervisorStdoutRead, out pipes.LauncherStdoutWrite,
                ref attributes, 0))
            {
                int error = Marshal.GetLastWin32Error();
                throw new NativeFailureException(error);
            }
            NativeBooleanResult stdoutParent = SetHandleInheritance(
                pipes.SupervisorStdoutRead, false);
            if (!stdoutParent.Success)
            {
                throw new NativeFailureException(stdoutParent.ErrorCode);
            }

            if (!CreatePipe(out pipes.SupervisorStderrRead, out pipes.LauncherStderrWrite,
                ref attributes, 0))
            {
                int error = Marshal.GetLastWin32Error();
                throw new NativeFailureException(error);
            }
            NativeBooleanResult stderrParent = SetHandleInheritance(
                pipes.SupervisorStderrRead, false);
            if (!stderrParent.Success)
            {
                throw new NativeFailureException(stderrParent.ErrorCode);
            }
            return pipes;
        }
        catch
        {
            CloseHandleChecked(pipes.LauncherStdinRead);
            CloseHandleChecked(pipes.SupervisorStdinWrite);
            CloseHandleChecked(pipes.SupervisorStdoutRead);
            CloseHandleChecked(pipes.LauncherStdoutWrite);
            CloseHandleChecked(pipes.SupervisorStderrRead);
            CloseHandleChecked(pipes.LauncherStderrWrite);
            throw;
        }
    }

    public static IntPtr CreateJob()
    {
        IntPtr job = CreateJobObjectW(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
        {
            int error = Marshal.GetLastWin32Error();
            throw new NativeFailureException(error);
        }
        return job;
    }

    public static void ConfigureJob(IntPtr job)
    {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION information =
            new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        uint length = (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation,
            ref information, length))
        {
            int error = Marshal.GetLastWin32Error();
            throw new NativeFailureException(error);
        }
    }

    public static JobLimitsResult VerifyJobLimits(IntPtr job)
    {
        JobLimitsResult result = new JobLimitsResult();
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION information =
            new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        uint returned;
        uint length = (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        bool success = QueryInformationJobObject(job, JobObjectExtendedLimitInformation,
            ref information, length, out returned);
        int error = success ? 0 : Marshal.GetLastWin32Error();
        result.Succeeded = success;
        result.ErrorCode = error;
        if (!success)
        {
            return result;
        }

        JOBOBJECT_BASIC_LIMIT_INFORMATION basic = information.BasicLimitInformation;
        result.Matches = basic.LimitFlags == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        return result;
    }

    public static JobAccountingResult GetAccounting(IntPtr job)
    {
        JobAccountingResult result = new JobAccountingResult();
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION information =
            new JOBOBJECT_BASIC_ACCOUNTING_INFORMATION();
        uint returned;
        uint length = (uint)Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
        bool success = QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
            ref information, length, out returned);
        int error = success ? 0 : Marshal.GetLastWin32Error();
        result.Succeeded = success;
        result.ErrorCode = error;
        if (success)
        {
            result.TotalProcesses = information.TotalProcesses;
            result.ActiveProcesses = information.ActiveProcesses;
            result.TotalTerminatedProcesses = information.TotalTerminatedProcesses;
        }
        return result;
    }

    public static ProcessIdsResult GetProcessIds(IntPtr job)
    {
        ProcessIdsResult result = new ProcessIdsResult();
        int size = 4096;
        const int maximum = 1024 * 1024;
        while (size <= maximum)
        {
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                uint returned;
                bool success = QueryInformationJobObject(job, JobObjectBasicProcessIdList,
                    buffer, (uint)size, out returned);
                int error = success ? 0 : Marshal.GetLastWin32Error();
                if (!success)
                {
                    if ((error == ERROR_MORE_DATA || error == ERROR_INSUFFICIENT_BUFFER) &&
                        size < maximum)
                    {
                        size = Math.Min(size * 2, maximum);
                        continue;
                    }
                    result.ErrorCode = error;
                    return result;
                }

                uint count = unchecked((uint)Marshal.ReadInt32(buffer, sizeof(uint)));
                int maxCount = (size - (sizeof(uint) * 2)) / IntPtr.Size;
                if (count > (uint)maxCount)
                {
                    result.ErrorCode = ERROR_MORE_DATA;
                    if (size < maximum)
                    {
                        size = Math.Min(size * 2, maximum);
                        continue;
                    }
                    return result;
                }
                uint[] ids = new uint[count];
                for (int index = 0; index < (int)count; index++)
                {
                    IntPtr value = Marshal.ReadIntPtr(
                        buffer, sizeof(uint) * 2 + index * IntPtr.Size);
                    ids[index] = unchecked((uint)value.ToInt64());
                }
                result.Succeeded = true;
                result.ErrorCode = 0;
                result.ProcessIds = ids;
                return result;
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }
        result.ErrorCode = ERROR_MORE_DATA;
        return result;
    }

    public static ProcessHandleResult OpenQueryProcess(uint processId)
    {
        ProcessHandleResult result = new ProcessHandleResult();
        IntPtr handle = OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, false, processId);
        if (handle == IntPtr.Zero)
        {
            result.Succeeded = false;
            result.ErrorCode = Marshal.GetLastWin32Error();
            return result;
        }
        result.Succeeded = true;
        result.Handle = handle;
        result.ErrorCode = 0;
        return result;
    }

    public static ProcessLiveResult GetProcessLive(IntPtr processHandle)
    {
        ProcessLiveResult result = new ProcessLiveResult();
        uint exitCode;
        bool success = GetExitCodeProcess(processHandle, out exitCode);
        int error = success ? 0 : Marshal.GetLastWin32Error();
        result.Succeeded = success;
        result.ExitCode = exitCode;
        result.ErrorCode = error;
        result.Live = success && exitCode == STILL_ACTIVE;
        return result;
    }

    public static MembershipResult CheckMembership(IntPtr processHandle, IntPtr jobHandle)
    {
        MembershipResult result = new MembershipResult();
        bool member;
        bool success = IsProcessInJob(processHandle, jobHandle, out member);
        int error = success ? 0 : Marshal.GetLastWin32Error();
        result.Succeeded = success;
        result.IsMember = member;
        result.ErrorCode = error;
        return result;
    }

    public static ImageResult GetImage(IntPtr processHandle)
    {
        ImageResult result = new ImageResult();
        StringBuilder value = new StringBuilder(32768);
        uint length = (uint)value.Capacity;
        bool success = QueryFullProcessImageNameW(processHandle, 0, value, ref length);
        int error = success ? 0 : Marshal.GetLastWin32Error();
        result.Succeeded = success;
        result.ErrorCode = error;
        if (success)
        {
            result.ImagePath = value.ToString();
        }
        return result;
    }

    public static ProcessCreationResult CreateContainedProcess(
        string applicationName, StringBuilder commandLine, string currentDirectory,
        IntPtr attributeList, IntPtr stdinHandle, IntPtr stdoutHandle, IntPtr stderrHandle)
    {
        STARTUPINFOEX startup = new STARTUPINFOEX();
        startup.StartupInfo.cb = (uint)Marshal.SizeOf(typeof(STARTUPINFOEX));
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = stdinHandle;
        startup.StartupInfo.hStdOutput = stdoutHandle;
        startup.StartupInfo.hStdError = stderrHandle;
        startup.lpAttributeList = attributeList;
        PROCESS_INFORMATION processInfo;
        uint flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW;
        bool success = CreateProcessW(applicationName, commandLine, IntPtr.Zero, IntPtr.Zero,
            true, flags, IntPtr.Zero, currentDirectory, ref startup, out processInfo);
        int error = success ? 0 : Marshal.GetLastWin32Error();
        ProcessCreationResult result = new ProcessCreationResult();
        result.Succeeded = success;
        result.ErrorCode = error;
        if (success)
        {
            result.ProcessInfo = processInfo;
        }
        return result;
    }

    public static DrainWorker StartDrain(IntPtr handle)
    {
        return new DrainWorker(handle);
    }

    public static DurabilityResult WriteFlushBounded(
        FileStream stream, byte[] bytes, int timeoutMilliseconds)
    {
        DurabilityResult result = new DurabilityResult();
        Thread writer = new Thread(delegate()
        {
            try
            {
                stream.Position = 0;
                stream.SetLength(0);
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(true);
                result.Succeeded = true;
            }
            catch
            {
                result.ErrorCode = 0;
            }
        });
        writer.IsBackground = true;
        writer.Start();
        if (!writer.Join(timeoutMilliseconds))
        {
            result.TimedOut = true;
        }
        return result;
    }

    public static NativeBooleanResult TerminateJob(IntPtr job, uint exitCode)
    {
        NativeBooleanResult result = new NativeBooleanResult();
        bool success = TerminateJobObject(job, exitCode);
        result.Success = success;
        result.ErrorCode = success ? 0 : Marshal.GetLastWin32Error();
        return result;
    }

    public static WaitResult WaitProcess(IntPtr process, uint milliseconds)
    {
        uint value = WaitForSingleObject(process, milliseconds);
        WaitResult result = new WaitResult();
        result.Value = value;
        if (value == WAIT_FAILED)
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
        }
        return result;
    }

    public static ProcessMetadataResult QueryProcessMetadata(uint processId, int timeoutMilliseconds)
    {
        ProcessMetadataResult result = new ProcessMetadataResult();
        if (Interlocked.CompareExchange(ref observerBusy, 1, 0) != 0)
        {
            result.ProviderFailed = true;
            return result;
        }
        Thread queryThread = new Thread(delegate()
        {
            try
            {
                string query = "SELECT ParentProcessId, CommandLine FROM Win32_Process WHERE ProcessId = " +
                    processId.ToString(System.Globalization.CultureInfo.InvariantCulture);
                using (ManagementObjectSearcher searcher = new ManagementObjectSearcher(
                    "root\\cimv2", query))
                using (ManagementObjectCollection collection = searcher.Get())
                {
                    foreach (ManagementObject item in collection)
                    {
                        object parent = item["ParentProcessId"];
                        object command = item["CommandLine"];
                        result.ParentProcessId = parent == null ? 0 : Convert.ToUInt32(parent);
                        result.CommandLine = command == null ? String.Empty : (string)command;
                        result.Succeeded = true;
                        item.Dispose();
                        break;
                    }
                }
            }
            catch
            {
                result.ProviderFailed = true;
            }
            finally
            {
                Interlocked.Exchange(ref observerBusy, 0);
            }
        });
        queryThread.IsBackground = true;
        queryThread.Start();
        if (!queryThread.Join(timeoutMilliseconds))
        {
            result.TimedOut = true;
        }
        return result;
    }

    public static bool VerifyX64StructureSizes()
    {
        return IntPtr.Size == 8 &&
            Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)) == 24 &&
            Marshal.SizeOf(typeof(STARTUPINFO)) == 104 &&
            Marshal.SizeOf(typeof(STARTUPINFOEX)) == 112 &&
            Marshal.SizeOf(typeof(PROCESS_INFORMATION)) == 24 &&
            Marshal.SizeOf(typeof(JOBOBJECT_BASIC_LIMIT_INFORMATION)) == 64 &&
            Marshal.SizeOf(typeof(IO_COUNTERS)) == 48 &&
            Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)) == 144 &&
            Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)) == 48;
    }
}
'@

function Stop-EgSupervisor {
    param(
        [Parameter(Mandatory = $true)][string]$SupportRef,
        [int]$ErrorCode = 0,
        [switch]$ContainmentFailure,
        [switch]$EvidenceFailure
    )

    $script:EgState.support_ref = $SupportRef
    if ($ErrorCode -ne 0) {
        $script:EgState.error_code = [int]$ErrorCode
    }
    $script:EgState.precreate_rejection = -not $script:EgState.creation_attempted
    if ($ContainmentFailure) {
        $script:EgState.containment_failure = $true
    }
    if ($EvidenceFailure) {
        $script:EgState.evidence_integrity_failure = $true
    }
    throw (New-Object System.Exception($SupportRef))
}

function Test-EgUnsafeText {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.IndexOf([char]0) -ge 0 -or $Value.IndexOf([char]10) -ge 0 -or
        $Value.IndexOf([char]13) -ge 0 -or $Value.IndexOf('"') -ge 0) {
        return $true
    }
    foreach ($character in $Value.ToCharArray()) {
        if ([int][char]$character -lt 32) {
            return $true
        }
    }
    return $false
}

function Get-EgLocalAbsolutePath {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    if ([string]::IsNullOrEmpty($Value) -or $Value.Length -gt 8192 -or
        (Test-EgUnsafeText -Value $Value)) {
        Stop-EgSupervisor -SupportRef ('EG_SUPERVISOR_' + $Name.ToUpperInvariant() + '_INVALID')
    }
    if ($Value.StartsWith('\\', [System.StringComparison]::Ordinal) -or
        $Value.StartsWith('\\.\', [System.StringComparison]::Ordinal) -or
        $Value.StartsWith('\\?\', [System.StringComparison]::Ordinal) -or
        $Value.StartsWith('\??\', [System.StringComparison]::Ordinal)) {
        Stop-EgSupervisor -SupportRef ('EG_SUPERVISOR_' + $Name.ToUpperInvariant() + '_NONLOCAL')
    }
    try {
        if (-not [System.IO.Path]::IsPathRooted($Value)) {
            Stop-EgSupervisor -SupportRef ('EG_SUPERVISOR_' + $Name.ToUpperInvariant() + '_NOT_ABSOLUTE')
        }
        $absolute = [System.IO.Path]::GetFullPath($Value)
        $root = [System.IO.Path]::GetPathRoot($absolute)
        if ([string]::IsNullOrEmpty($root) -or $root.StartsWith('\\', [System.StringComparison]::Ordinal)) {
            Stop-EgSupervisor -SupportRef ('EG_SUPERVISOR_' + $Name.ToUpperInvariant() + '_NONLOCAL')
        }
        return $absolute
    }
    catch {
        Stop-EgSupervisor -SupportRef ('EG_SUPERVISOR_' + $Name.ToUpperInvariant() + '_INVALID')
    }
}

function ConvertTo-EgSafePowerShellLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Length -gt 8192 -or (Test-EgUnsafeText -Value $Value)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_SERIALISED_INPUT_INVALID'
    }
    return ("'" + $Value.Replace("'", "''") + "'")
}

function ConvertTo-EgNativeCommandLine {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Argument)

    $builder = New-Object System.Text.StringBuilder
    for ($argumentIndex = 0; $argumentIndex -lt $Argument.Count; $argumentIndex++) {
        if ($argumentIndex -gt 0) { [void]$builder.Append(' ') }
        $item = [string]$Argument[$argumentIndex]
        if (Test-EgUnsafeText -Value $item) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_SERIALISED_INPUT_INVALID'
        }
        [void]$builder.Append('"')
        $backslashes = 0
        foreach ($character in $item.ToCharArray()) {
            if ($character -eq '\') {
                $backslashes++
                continue
            }
            if ($character -eq '"') {
                [void]$builder.Append(('\' * (2 * $backslashes + 1)))
                [void]$builder.Append('"')
                $backslashes = 0
                continue
            }
            if ($backslashes -gt 0) {
                [void]$builder.Append(('\' * $backslashes))
                $backslashes = 0
            }
            [void]$builder.Append($character)
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * (2 * $backslashes)))
        }
        [void]$builder.Append('"')
    }
    if ($builder.Length -gt 32766) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_COMMAND_LINE_TOO_LONG'
    }
    return $builder.ToString()
}

function ConvertTo-EgUtf8JsonBytes {
    param([Parameter(Mandatory = $true)]$Object)

    $json = $Object | ConvertTo-Json -Depth 12 -Compress
    $text = [string]$json + "`n"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $bytes = $encoding.GetBytes($text)
    if ($bytes.Length -gt 65536) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_TOO_LARGE' -EvidenceFailure
    }
    return $bytes
}

function Initialize-EgNative {
    $typeName = [System.Management.Automation.PSTypeName]'EnergyGridOneShotSupervisorNative'
    if ($null -eq $typeName.Type) {
        try {
            [void](Add-Type -TypeDefinition $script:EgNativeSource `
                -ReferencedAssemblies @('System.Management.dll') -ErrorAction Stop)
        }
        catch {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_NATIVE_COMPILE_FAILED'
        }
    }
    [EnergyGridOneShotSupervisorNative]::ResetControlState()
    if (-not [EnergyGridOneShotSupervisorNative]::VerifyX64StructureSizes()) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_X64_LAYOUT_INVALID'
    }
    $handlerError = 0
    if (-not [EnergyGridOneShotSupervisorNative]::InstallConsoleHandler([ref]$handlerError)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_CONSOLE_HANDLER_FAILED' -ErrorCode $handlerError
    }
}

function Test-EgEvidenceRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_ROOT_INVALID'
    }
    try {
        $item = Get-Item -LiteralPath $Path -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_ROOT_REPARSE'
        }
        $driveRoot = [System.IO.Path]::GetPathRoot($Path)
        $drive = New-Object System.IO.DriveInfo($driveRoot)
        if ($drive.DriveFormat -cne 'NTFS') {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_ROOT_NOT_NTFS'
        }
        $acl = Get-Acl -LiteralPath $Path
        if (-not $acl.AreAccessRulesProtected) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_ROOT_ACL_INHERITED'
        }
        $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $allowed = @($currentSid, 'S-1-5-18', 'S-1-5-32-544')
        $seen = @{}
        foreach ($rule in $acl.GetAccessRules($true, $false,
            [System.Security.Principal.SecurityIdentifier])) {
            $sid = $rule.IdentityReference.Value
            if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
                $allowed -notcontains $sid) {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_ROOT_ACL_UNAUTHORISED'
            }
            $seen[$sid] = $true
        }
        foreach ($sid in $allowed) {
            if (-not $seen.ContainsKey($sid)) {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_ROOT_ACL_INCOMPLETE'
            }
        }
    }
    catch {
        if ($_.Exception.Message -like 'EG_SUPERVISOR_*') { throw }
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EVIDENCE_ROOT_INVALID'
    }
}

function Close-EgHandle {
    param([Parameter(Mandatory = $true)][IntPtr]$Handle)

    if ($Handle -eq [IntPtr]::Zero) { return }
    $result = [EnergyGridOneShotSupervisorNative]::CloseHandleChecked($Handle)
    if (-not $result.Success -and -not $script:EgState.containment_failure) {
        $script:EgState.containment_failure = $true
        $script:EgState.support_ref = 'EG_SUPERVISOR_HANDLE_CLOSE_FAILED'
        if ($result.ErrorCode -ne 0) { $script:EgState.error_code = $result.ErrorCode }
    }
}

function Set-EgInputContract {
    $script:EgLauncherPathNormal = Get-EgLocalAbsolutePath -Name 'launcher_path' -Value $LauncherPath
    $script:EgConfigPathNormal = Get-EgLocalAbsolutePath -Name 'config_path' -Value $ConfigPath
    $script:EgPythonExeNormal = Get-EgLocalAbsolutePath -Name 'python_exe' -Value $PythonExe
    $script:EgCheckoutRootNormal = Get-EgLocalAbsolutePath -Name 'checkout_root' -Value $CheckoutRoot
    $script:EgCredentialPathNormal = Get-EgLocalAbsolutePath -Name 'credential_path' -Value $CredentialPath
    $script:EgBrowserCachePathNormal = Get-EgLocalAbsolutePath -Name 'browser_cache_path' -Value $BrowserCachePath
    $script:EgLogRootNormal = Get-EgLocalAbsolutePath -Name 'log_root' -Value $LogRoot
    $script:EgEvidenceRootNormal = Get-EgLocalAbsolutePath -Name 'evidence_root' -Value $EvidenceRoot

    if ([System.IO.Path]::GetFileName($script:EgLauncherPathNormal) -ine 'launcher.ps1') {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_LAUNCHER_IDENTITY_INVALID'
    }
    $script:EgLauncherLibraryNormal = Join-Path ([System.IO.Path]::GetDirectoryName($script:EgLauncherPathNormal)) 'launcher_lib.ps1'
    if ([System.IO.Path]::GetFileName($script:EgLauncherLibraryNormal) -cne 'launcher_lib.ps1') {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_LAUNCHER_LIBRARY_IDENTITY_INVALID'
    }
    if ($ExpectedLauncherSha256 -notmatch '^[0-9a-fA-F]{64}$' -or
        $ExpectedLauncherLibrarySha256 -notmatch '^[0-9a-fA-F]{64}$') {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_HASH_FORMAT_INVALID'
    }
    if ($RunId -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_RUN_ID_INVALID'
    }
    if ($AuthorisedLauncherRootWriteSid.Count -lt 1 -or
        $AuthorisedLauncherRootWriteSid.Count -gt 64) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_SID_SET_INVALID'
    }
    foreach ($sid in $AuthorisedLauncherRootWriteSid) {
        if ($sid.Length -gt 184 -or $sid -notmatch '^S-[0-9]+-[0-9]+(?:-[0-9]+)+$' -or
            (Test-EgUnsafeText -Value $sid)) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_SID_SET_INVALID'
        }
    }
    if ($ExpectedBranch.Length -gt 4096 -or (Test-EgUnsafeText -Value $ExpectedBranch)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_EXPECTED_BRANCH_INVALID'
    }
    if (-not (Test-Path -LiteralPath $script:EgLauncherPathNormal -PathType Leaf) -or
        -not (Test-Path -LiteralPath $script:EgLauncherLibraryNormal -PathType Leaf)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_LAUNCHER_MISSING'
    }
    if (-not (Test-Path -LiteralPath $script:EgCheckoutRootNormal -PathType Container)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_CHECKOUT_ROOT_INVALID'
    }
    if (-not (Test-Path -LiteralPath $script:EgPythonExeNormal -PathType Leaf)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_PYTHON_EXE_INVALID'
    }
    if (-not (Test-Path -LiteralPath ([System.Environment]::SystemDirectory) -PathType Container)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_SYSTEM_DIRECTORY_INVALID'
    }
    $script:EgNativePowerShell = Join-Path ([System.Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $script:EgNativePowerShell -PathType Leaf)) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_WINDOWS_POWERSHELL_MISSING'
    }
}

function Test-EgShellContract {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        $PSVersionTable.PSEdition -cne 'Desktop' -or
        $PSVersionTable.PSVersion.Major -ne 5 -or
        $PSVersionTable.PSVersion.Minor -ne 1 -or
        [IntPtr]::Size -ne 8) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_UNSUPPORTED_SHELL'
    }
}

function Test-EgLauncherHashes {
    try {
        $launcherHash = (Get-FileHash -LiteralPath $script:EgLauncherPathNormal -Algorithm SHA256).Hash
        $libraryHash = (Get-FileHash -LiteralPath $script:EgLauncherLibraryNormal -Algorithm SHA256).Hash
    }
    catch {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_LAUNCHER_HASH_READ_FAILED'
    }
    if ($launcherHash -ine $ExpectedLauncherSha256) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_LAUNCHER_HASH_MISMATCH'
    }
    if ($libraryHash -ine $ExpectedLauncherLibrarySha256) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_LAUNCHER_LIBRARY_HASH_MISMATCH'
    }
}

function New-EgLauncherWrapper {
    $sidLiterals = New-Object 'System.Collections.Generic.List[string]'
    foreach ($sid in $AuthorisedLauncherRootWriteSid) {
        [void]$sidLiterals.Add((ConvertTo-EgSafePowerShellLiteral -Value $sid))
    }
    $sidArray = '@(' + ($sidLiterals -join ',') + ')'
    $pairs = @(
        "ConfigPath = $(ConvertTo-EgSafePowerShellLiteral -Value $script:EgConfigPathNormal)",
        "PythonExe = $(ConvertTo-EgSafePowerShellLiteral -Value $script:EgPythonExeNormal)",
        "CheckoutRoot = $(ConvertTo-EgSafePowerShellLiteral -Value $script:EgCheckoutRootNormal)",
        "CredentialPath = $(ConvertTo-EgSafePowerShellLiteral -Value $script:EgCredentialPathNormal)",
        "BrowserCachePath = $(ConvertTo-EgSafePowerShellLiteral -Value $script:EgBrowserCachePathNormal)",
        "ExpectedBranch = $(ConvertTo-EgSafePowerShellLiteral -Value $ExpectedBranch)",
        "AuthorisedLauncherRootWriteSid = $sidArray",
        "Command = 'run'",
        "LogRoot = $(ConvertTo-EgSafePowerShellLiteral -Value $script:EgLogRootNormal)",
        "RunId = $(ConvertTo-EgSafePowerShellLiteral -Value $RunId)"
    )
    $launcherLiteral = ConvertTo-EgSafePowerShellLiteral -Value $script:EgLauncherPathNormal
    $wrapper = "`$launcher = $launcherLiteral; `$parameters = [ordered]@{ " +
        ($pairs -join '; ') + " }; & `$launcher @parameters; exit `$LASTEXITCODE"
    if ($wrapper.Length -gt 24576) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_SERIALISED_INPUT_TOO_LARGE'
    }
    return $wrapper
}

function Write-EgReservedIntent {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileStream]$Stream,
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][int]$TimeoutMilliseconds
    )

    try {
        $handleResult = [EnergyGridOneShotSupervisorNative]::SetHandleInheritance(
            $Stream.SafeFileHandle.DangerousGetHandle(), $false)
        if (-not $handleResult.Success) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_INTENT_HANDLE_FAILED' `
                -ErrorCode $handleResult.ErrorCode -EvidenceFailure
        }
        $durability = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
            $Stream, $Bytes, $TimeoutMilliseconds)
        if (-not $durability.Succeeded) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_INTENT_FLUSH_FAILED' `
                -ErrorCode $durability.ErrorCode -EvidenceFailure
        }
        $script:EgState.intent_bytes = $Bytes
        $script:EgState.intent_committed = $true
    }
    catch {
        if ($_.Exception.Message -like 'EG_SUPERVISOR_*') { throw }
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_INTENT_FLUSH_FAILED' -EvidenceFailure
    }
}

function Get-EgIntentObject {
    return [ordered]@{
        schema = 'energygrid.one_shot_supervisor.intent.v1'
        run_id = $RunId
        operation = 'run'
        launcher_identity = 'launcher.ps1'
        launcher_library_identity = 'launcher_lib.ps1'
        launcher_sha256 = $ExpectedLauncherSha256.ToLowerInvariant()
        launcher_library_sha256 = $ExpectedLauncherLibrarySha256.ToLowerInvariant()
        expected_branch = $ExpectedBranch
        timeout_seconds = [int]$TimeoutSeconds
        containment = 'creation_time_job_object'
        job_identity = 'unnamed_job_object'
        job_limit = 'kill_on_job_close_only'
        creation_flags = @('EXTENDED_STARTUPINFO_PRESENT', 'CREATE_SUSPENDED', 'CREATE_NO_WINDOW')
        operation_command = 'run'
        raw_stream_retained_bytes = [uint64]0
        created_utc = [DateTime]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
    }
}

function Get-EgOutcomeObject {
    param([Parameter(Mandatory = $true)][string]$StartVerdict)

    $intentHash = $null
    if ($script:EgState.intent_committed) {
        $sha = New-Object System.Security.Cryptography.SHA256Managed
        try { $intentHash = ([BitConverter]::ToString($sha.ComputeHash($script:EgState.intent_bytes))).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
    }
    return [ordered]@{
        schema = 'energygrid.one_shot_supervisor.outcome.v1'
        run_id = $RunId
        operation = 'run'
        intent_sha256 = $intentHash
        start_verdict = $StartVerdict
        launcher_exit_code = $script:EgState.launcher_exit_code
        creation_attempted = [bool]$script:EgState.creation_attempted
        resume_attempted = [bool]$script:EgState.resume_attempted
        application_child_observed = [bool]$script:EgState.application_child_observed
        application_child_observation_elapsed_ms = $script:EgState.application_child_observation_elapsed_ms
        total_processes = $script:EgState.total_processes
        active_processes = $script:EgState.active_processes
        total_terminated_processes = $script:EgState.total_terminated_processes
        reap_confirmed = [bool]$script:EgState.reap_confirmed
        stdout_bytes = [uint64]$script:EgState.stdout_bytes
        stderr_bytes = [uint64]$script:EgState.stderr_bytes
        stdout_complete = [bool]$script:EgState.stdout_complete
        stderr_complete = [bool]$script:EgState.stderr_complete
        raw_stream_retained_bytes = [uint64]0
        containment = if ($script:EgState.reap_confirmed) { 'REAP_CONFIRMED' } else { 'REAP_UNPROVEN' }
        completed_utc = [DateTime]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
    }
}

function Write-EgOutcome {
    param([Parameter(Mandatory = $true)][string]$StartVerdict)

    $script:EgState.outcome_write_attempted = $true
    $outcomePath = Join-Path $script:EgEvidenceRootNormal ($RunId + '.outcome.json')
    try {
        $bytes = ConvertTo-EgUtf8JsonBytes -Object (Get-EgOutcomeObject -StartVerdict $StartVerdict)
        $stream = New-Object System.IO.FileStream(
            $outcomePath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::WriteThrough)
        try {
            $handleResult = [EnergyGridOneShotSupervisorNative]::SetHandleInheritance(
                $stream.SafeFileHandle.DangerousGetHandle(), $false)
            if (-not $handleResult.Success) {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_OUTCOME_HANDLE_FAILED' `
                    -ErrorCode $handleResult.ErrorCode -EvidenceFailure
            }
            $durability = [EnergyGridOneShotSupervisorNative]::WriteFlushBounded(
                $stream, $bytes, 5000)
            if (-not $durability.Succeeded) {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED' `
                    -ErrorCode $durability.ErrorCode -EvidenceFailure
            }
            $script:EgState.outcome_committed = $true
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    catch {
        $script:EgState.evidence_integrity_failure = $true
        if ($_.Exception.PSObject.Properties['ErrorCode']) {
            $script:EgState.error_code = [int]$_.Exception.ErrorCode
        }
        if ($_.Exception.Message -like 'EG_SUPERVISOR_*') {
            $script:EgState.support_ref = $_.Exception.Message
        }
        else {
            $script:EgState.support_ref = 'EG_SUPERVISOR_OUTCOME_FLUSH_FAILED'
        }
    }
}

function Get-EgCanonicalApplicationCommandLine {
    return (ConvertTo-EgNativeCommandLine -Argument @(
        $script:EgPythonExeNormal, '-m', 'energygrid_bill_downloader', 'run', '--config',
        $script:EgConfigPathNormal
    ))
}

function Test-EgApplicationChild {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][IntPtr]$LauncherHandle,
        [Parameter(Mandatory = $true)][uint32]$LauncherPid,
        [Parameter(Mandatory = $true)][long]$StartTicks
    )

    $observerDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        [int64]([System.Diagnostics.Stopwatch]::Frequency / 20))
    $pidResult = [EnergyGridOneShotSupervisorNative]::GetProcessIds($JobHandle)
    if (-not $pidResult.Succeeded) {
        $script:EgState.observer_failed = $true
        return $false
    }
    $expectedImage = [System.IO.Path]::GetFullPath($script:EgPythonExeNormal)
    $expectedCommandLine = Get-EgCanonicalApplicationCommandLine
    foreach ($candidatePid in $pidResult.ProcessIds) {
        if ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $observerDeadline) { break }
        if ([uint32]$candidatePid -eq $LauncherPid) { continue }
        $candidate = [EnergyGridOneShotSupervisorNative]::OpenQueryProcess([uint32]$candidatePid)
        if (-not $candidate.Succeeded) {
            $script:EgState.observer_failed = $true
            continue
        }
        try {
            $live = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            if (-not $live.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $live.Live) { continue }
            $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $membership.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $membership.IsMember) { continue }
            $launcherLive = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            if (-not $launcherLive.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLive.Live) { continue }
            $image = [EnergyGridOneShotSupervisorNative]::GetImage($candidate.Handle)
            if (-not $image.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if ([System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath($image.ImagePath), $expectedImage) -eq $false) { continue }
            $metadata = [EnergyGridOneShotSupervisorNative]::QueryProcessMetadata(
                [uint32]$candidatePid, 10)
            if (-not $metadata.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if ([uint32]$metadata.ParentProcessId -ne $LauncherPid) { continue }
            if ($metadata.CommandLine -cne $expectedCommandLine) { continue }
            $launcherLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($LauncherHandle)
            $candidateLiveAgain = [EnergyGridOneShotSupervisorNative]::GetProcessLive($candidate.Handle)
            $membershipAgain = [EnergyGridOneShotSupervisorNative]::CheckMembership(
                $candidate.Handle, $JobHandle)
            if (-not $launcherLiveAgain.Succeeded -or
                -not $candidateLiveAgain.Succeeded -or
                -not $membershipAgain.Succeeded) {
                $script:EgState.observer_failed = $true
                continue
            }
            if (-not $launcherLiveAgain.Live -or -not $candidateLiveAgain.Live -or
                -not $membershipAgain.IsMember) { continue }
            $elapsed = [Math]::Floor((([System.Diagnostics.Stopwatch]::GetTimestamp() -
                $StartTicks) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency)
            $script:EgState.application_child_observation_elapsed_ms = [int64]$elapsed
            return $true
        }
        finally {
            Close-EgHandle -Handle $candidate.Handle
        }
    }
    return $false
}

function Get-EgAccounting {
    param([Parameter(Mandatory = $true)][IntPtr]$JobHandle)

    $accounting = [EnergyGridOneShotSupervisorNative]::GetAccounting($JobHandle)
    if (-not $accounting.Succeeded) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_ACCOUNTING_FAILED' `
            -ErrorCode $accounting.ErrorCode -ContainmentFailure
    }
    $script:EgState.total_processes = [uint64]$accounting.TotalProcesses
    $script:EgState.active_processes = [uint64]$accounting.ActiveProcesses
    $script:EgState.total_terminated_processes = [uint64]$accounting.TotalTerminatedProcesses
    return $accounting
}

function Invoke-EgTerminateJob {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][string]$Reason
    )

    if ($script:EgState.termination_started) { return }
    $script:EgState.termination_started = $true
    if ($Reason -eq 'TIMEOUT') { $script:EgState.timed_out = $true }
    if ($Reason -eq 'INTERRUPTION') { $script:EgState.interrupted = $true }
    [EnergyGridOneShotSupervisorNative]::RequestFailure()
    $termination = [EnergyGridOneShotSupervisorNative]::TerminateJob(
        $JobHandle, [EnergyGridOneShotSupervisorNative]::SUPERVISOR_TERMINATION_EXIT_CODE)
    if ($termination.Success) {
        $script:EgState.termination_succeeded = $true
    }
    else {
        $script:EgState.termination_failure = $true
        $script:EgState.containment_failure = $true
        $script:EgState.support_ref = 'EG_SUPERVISOR_TERMINATION_FAILED'
        $script:EgState.error_code = $termination.ErrorCode
    }
}

function Get-EgDeadlineTicks {
    param([Parameter(Mandatory = $true)][long]$StartTicks,[Parameter(Mandatory = $true)][int]$Seconds)
    return [int64]($StartTicks + ([int64]$Seconds * [int64][System.Diagnostics.Stopwatch]::Frequency))
}

function Get-EgDurabilityMilliseconds {
    param([Parameter(Mandatory = $true)][long]$StartTicks,[Parameter(Mandatory = $true)][int]$Seconds)
    $maximum = 5000
    if ($StartTicks -eq 0) { return $maximum }
    $deadline = Get-EgDeadlineTicks -StartTicks $StartTicks -Seconds $Seconds
    $remainingTicks = $deadline - [System.Diagnostics.Stopwatch]::GetTimestamp()
    if ($remainingTicks -le 0) { return 0 }
    $remaining = [int][Math]::Floor(($remainingTicks * 1000.0) /
        [System.Diagnostics.Stopwatch]::Frequency)
    return [Math]::Min($maximum, [Math]::Max(0, $remaining))
}

function Test-EgDeadlineReached {
    param([Parameter(Mandatory = $true)][long]$DeadlineTicks)
    return ([System.Diagnostics.Stopwatch]::GetTimestamp() -ge $DeadlineTicks)
}

function Wait-EgReap {
    param(
        [Parameter(Mandatory = $true)][IntPtr]$JobHandle,
        [Parameter(Mandatory = $true)][long]$DeadlineTicks,
        [Parameter(Mandatory = $true)][int]$WindowSeconds
    )

    $windowDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]$WindowSeconds * [int64][System.Diagnostics.Stopwatch]::Frequency))
    while ([System.Diagnostics.Stopwatch]::GetTimestamp() -lt $windowDeadline) {
        try {
            $accounting = Get-EgAccounting -JobHandle $JobHandle
        }
        catch {
            $script:EgState.containment_failure = $true
            break
        }
        if ($accounting.ActiveProcesses -eq 0) {
            $script:EgState.reap_confirmed = $true
            return
        }
        Start-Sleep -Milliseconds 100
    }
    try {
        $final = Get-EgAccounting -JobHandle $JobHandle
        if ($final.ActiveProcesses -eq 0) {
            $script:EgState.reap_confirmed = $true
        }
        else {
            $script:EgState.reap_confirmed = $false
        }
    }
    catch {
        $script:EgState.reap_confirmed = $false
        $script:EgState.containment_failure = $true
    }
}

function Wait-EgDrains {
    param(
        [Parameter(Mandatory = $true)]$StdoutDrain,
        [Parameter(Mandatory = $true)]$StderrDrain
    )

    $deadline = [System.Diagnostics.Stopwatch]::GetTimestamp() +
        ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency)
    while ((-not $StdoutDrain.Completed -or -not $StderrDrain.Completed) -and
        [System.Diagnostics.Stopwatch]::GetTimestamp() -lt $deadline) {
        [void]$StdoutDrain.Join(100)
        [void]$StderrDrain.Join(100)
    }
    $script:EgState.stdout_bytes = [uint64]$StdoutDrain.Bytes
    $script:EgState.stderr_bytes = [uint64]$StderrDrain.Bytes
    $script:EgState.stdout_complete = [bool]$StdoutDrain.Completed
    $script:EgState.stderr_complete = [bool]$StderrDrain.Completed
    if ($StdoutDrain.Failed -or $StderrDrain.Failed -or
        -not $script:EgState.stdout_complete -or -not $script:EgState.stderr_complete) {
        $script:EgState.drain_failure = $true
    }
}

function Get-EgStartVerdict {
    if ($script:EgState.evidence_integrity_failure -or $script:EgState.containment_failure -or
        $script:EgState.observer_failed) {
        return 'AMBIGUOUS'
    }
    if ((-not $script:EgState.creation_attempted) -and $script:EgState.precreate_rejection) {
        return 'NOT_STARTED_PROVEN'
    }
    if ($script:EgState.creation_succeeded -and
        $script:EgState.baseline_total_processes -eq 1 -and
        $script:EgState.baseline_active_processes -eq 1 -and
        $script:EgState.total_processes -eq 1 -and
        $script:EgState.active_processes -eq 0 -and
        -not $script:EgState.application_child_observed -and
        $script:EgState.reap_confirmed) {
        return 'NOT_STARTED_PROVEN'
    }
    if ($script:EgState.application_child_observed -and
        $script:EgState.total_processes -ge 2 -and
        $script:EgState.reap_confirmed -and
        $script:EgState.intent_committed) {
        return 'STARTED_PROVEN'
    }
    return 'AMBIGUOUS'
}

function Get-EgExitCode {
    if (-not $script:EgState.outcome_committed) { return 3 }
    if ($script:EgState.containment_failure -or $script:EgState.evidence_integrity_failure -or
        $script:EgState.drain_failure -or $script:EgState.termination_failure -or
        $script:EgState.observer_failed) { return 3 }
    if ($script:EgState.creation_succeeded -and -not $script:EgState.reap_confirmed) {
        return 3
    }
    if ($script:EgState.creation_succeeded -and
        (-not $script:EgState.stdout_complete -or -not $script:EgState.stderr_complete)) {
        return 3
    }
    if ($script:EgState.timed_out -or $script:EgState.interrupted) { return 2 }
    if ($script:EgState.start_verdict -eq 'AMBIGUOUS') { return 4 }
    if ($script:EgState.start_verdict -eq 'STARTED_PROVEN' -and
        $script:EgState.creation_succeeded -and
        $script:EgState.launcher_exit_code -eq 0 -and
        $script:EgState.reap_confirmed -and
        $script:EgState.stdout_complete -and $script:EgState.stderr_complete) { return 0 }
    return 1
}

function Write-EgPublicProjection {
    param([Parameter(Mandatory = $true)][bool]$ReceiptAvailable)

    if (-not $ReceiptAvailable) {
        $failure = [ordered]@{
            support_ref = [string]$script:EgState.support_ref
            start_verdict = 'AMBIGUOUS'
        }
        Write-Output (($failure | ConvertTo-Json -Compress))
        return
    }
    $projection = [ordered]@{
        schema = 'energygrid.one_shot_supervisor.public.v1'
        support_ref = [string]$script:EgState.support_ref
        error_code = $script:EgState.error_code
        start_verdict = [string]$script:EgState.start_verdict
        creation_attempted = [bool]$script:EgState.creation_attempted
        resume_attempted = [bool]$script:EgState.resume_attempted
        launcher_exit_code = $script:EgState.launcher_exit_code
        application_child_observed = [bool]$script:EgState.application_child_observed
        application_child_observation_elapsed_ms = $script:EgState.application_child_observation_elapsed_ms
        total_processes = $script:EgState.total_processes
        active_processes = $script:EgState.active_processes
        total_terminated_processes = $script:EgState.total_terminated_processes
        reap_confirmed = [bool]$script:EgState.reap_confirmed
        stdout_bytes = [uint64]$script:EgState.stdout_bytes
        stderr_bytes = [uint64]$script:EgState.stderr_bytes
        stdout_complete = [bool]$script:EgState.stdout_complete
        stderr_complete = [bool]$script:EgState.stderr_complete
        raw_stream_retained_bytes = [uint64]0
        outcome_committed = [bool]$script:EgState.outcome_committed
    }
    Write-Output (($projection | ConvertTo-Json -Compress))
}

$jobHandle = [IntPtr]::Zero
$processHandle = [IntPtr]::Zero
$threadHandle = [IntPtr]::Zero
$attributeResources = $null
$pipes = $null
$intentStream = $null
$stdoutDrain = $null
$stderrDrain = $null
$creationStartTicks = [int64]0
$deadlineTicks = [int64]0
$evidenceReady = $false
$terminalAccountingReady = $false

try {
    Test-EgShellContract
    Set-EgInputContract
    Initialize-EgNative
    Test-EgEvidenceRoot -Path $script:EgEvidenceRootNormal
    Test-EgLauncherHashes

    $intentPath = Join-Path $script:EgEvidenceRootNormal ($RunId + '.intent.json')
    try {
        $intentStream = New-Object System.IO.FileStream(
            $intentPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::WriteThrough)
    }
    catch [System.IO.IOException] {
        $script:EgState.duplicate_run_id = $true
        $script:EgState.support_ref = 'EG_SUPERVISOR_DUPLICATE_RUN_ID'
        $script:EgState.evidence_integrity_failure = $true
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_DUPLICATE_RUN_ID' -EvidenceFailure
    }
    $evidenceReady = $true

    $jobHandle = [EnergyGridOneShotSupervisorNative]::CreateJob()
    [EnergyGridOneShotSupervisorNative]::ConfigureJob($jobHandle)
    $limits = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($jobHandle)
    if (-not $limits.Succeeded) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_JOB_LIMIT_READBACK_FAILED' `
            -ErrorCode $limits.ErrorCode
    }
    if (-not $limits.Matches) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_JOB_LIMIT_READBACK_MISMATCH'
    }

    $pipes = [EnergyGridOneShotSupervisorNative]::CreatePipes()
    $attributeResources = New-Object EnergyGridOneShotSupervisorNative+AttributeResources(
        $jobHandle, $pipes.LauncherStdinRead, $pipes.LauncherStdoutWrite,
        $pipes.LauncherStderrWrite)
    $wrapper = New-EgLauncherWrapper
    # EncodedCommand is UTF-16LE, as required by Windows PowerShell's native contract.
    $encodedWrapper = [Convert]::ToBase64String(
        ([Text.Encoding]::Unicode.GetBytes($wrapper)))
    $commandLine = ConvertTo-EgNativeCommandLine -Argument @(
        'powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', $encodedWrapper
    )
    $commandBuilder = New-Object System.Text.StringBuilder($commandLine)
    $creationStartTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
    $deadlineTicks = Get-EgDeadlineTicks -StartTicks $creationStartTicks -Seconds $TimeoutSeconds
    $script:EgState.creation_attempted = $true
    $creation = [EnergyGridOneShotSupervisorNative]::CreateContainedProcess(
        $script:EgNativePowerShell, $commandBuilder, $script:EgCheckoutRootNormal,
        $attributeResources.AttributeList, $pipes.LauncherStdinRead,
        $pipes.LauncherStdoutWrite, $pipes.LauncherStderrWrite)
    if (-not $creation.Succeeded) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_CREATE_PROCESS_FAILED' `
            -ErrorCode $creation.ErrorCode
    }
    $script:EgState.creation_succeeded = $true
    $processHandle = $creation.ProcessInfo.hProcess
    $threadHandle = $creation.ProcessInfo.hThread

    $attributeResources.Dispose()
    $attributeResources = $null
    Close-EgHandle -Handle $pipes.LauncherStdinRead
    $pipes.LauncherStdinRead = [IntPtr]::Zero
    Close-EgHandle -Handle $pipes.LauncherStdoutWrite
    $pipes.LauncherStdoutWrite = [IntPtr]::Zero
    Close-EgHandle -Handle $pipes.LauncherStderrWrite
    $pipes.LauncherStderrWrite = [IntPtr]::Zero

    $stdoutDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStdoutRead)
    $pipes.SupervisorStdoutRead = [IntPtr]::Zero
    $stderrDrain = [EnergyGridOneShotSupervisorNative]::StartDrain($pipes.SupervisorStderrRead)
    $pipes.SupervisorStderrRead = [IntPtr]::Zero
    Close-EgHandle -Handle $pipes.SupervisorStdinWrite
    $pipes.SupervisorStdinWrite = [IntPtr]::Zero

    $membership = [EnergyGridOneShotSupervisorNative]::CheckMembership($processHandle, $jobHandle)
    if (-not $membership.Succeeded) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_MEMBERSHIP_CHECK_FAILED' `
            -ErrorCode $membership.ErrorCode -ContainmentFailure
    }
    if (-not $membership.IsMember) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_MEMBERSHIP_REJECTED' -ContainmentFailure
    }
    $baseline = Get-EgAccounting -JobHandle $jobHandle
    $script:EgState.baseline_total_processes = [uint64]$baseline.TotalProcesses
    $script:EgState.baseline_active_processes = [uint64]$baseline.ActiveProcesses
    if ($baseline.TotalProcesses -ne 1 -or $baseline.ActiveProcesses -ne 1) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_BASELINE_ACCOUNTING_INVALID' -ContainmentFailure
    }
    $limitsAfter = [EnergyGridOneShotSupervisorNative]::VerifyJobLimits($jobHandle)
    if (-not $limitsAfter.Succeeded -or -not $limitsAfter.Matches) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_JOB_LIMIT_POST_CREATE_INVALID' `
            -ErrorCode $limitsAfter.ErrorCode -ContainmentFailure
    }

    $intentBytes = ConvertTo-EgUtf8JsonBytes -Object (Get-EgIntentObject)
    $durabilityMilliseconds = Get-EgDurabilityMilliseconds -StartTicks $creationStartTicks `
        -Seconds $TimeoutSeconds
    if ($durabilityMilliseconds -le 0) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_INTENT_TIMEOUT' -ContainmentFailure -EvidenceFailure
    }
    Write-EgReservedIntent -Stream $intentStream -Bytes $intentBytes `
        -TimeoutMilliseconds $durabilityMilliseconds
    if (-not [EnergyGridOneShotSupervisorNative]::CommitIntent()) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_RESUME_GATE_CLOSED' -ContainmentFailure
    }
    $resume = [EnergyGridOneShotSupervisorNative]::TryResumeThread($threadHandle, $deadlineTicks)
    $script:EgState.resume_attempted = [bool]$resume.Attempted
    $script:EgState.resume_succeeded = [bool]$resume.Accepted
    if ($resume.ErrorCode -ne 0) { $script:EgState.error_code = $resume.ErrorCode }
    if ($resume.DeadlineExpired) {
        $script:EgState.timed_out = $true
        Invoke-EgTerminateJob -JobHandle $jobHandle -Reason 'TIMEOUT'
    }
    elseif (-not $resume.Attempted -or -not $resume.Accepted -or $resume.ReturnValue -ne 1) {
        Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_RESUME_ANOMALY' `
            -ErrorCode $resume.ErrorCode -ContainmentFailure
    }

    $launcherSignaled = [bool]$resume.DeadlineExpired
    while (-not $launcherSignaled) {
        if ([EnergyGridOneShotSupervisorNative]::IsTerminationRequested) {
            Invoke-EgTerminateJob -JobHandle $jobHandle -Reason 'INTERRUPTION'
            break
        }
        if (Test-EgDeadlineReached -DeadlineTicks $deadlineTicks) {
            Invoke-EgTerminateJob -JobHandle $jobHandle -Reason 'TIMEOUT'
            break
        }
        if (-not $script:EgState.application_child_observed) {
            if (Test-EgApplicationChild -JobHandle $jobHandle -LauncherHandle $processHandle `
                -LauncherPid $creation.ProcessInfo.dwProcessId -StartTicks $creationStartTicks) {
                $script:EgState.application_child_observed = $true
            }
        }
        $accounting = Get-EgAccounting -JobHandle $jobHandle
        $waitResult = [EnergyGridOneShotSupervisorNative]::WaitProcess($processHandle, 50)
        if ($waitResult.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_OBJECT_0) {
            $launcherSignaled = $true
            $exitResult = [EnergyGridOneShotSupervisorNative]::GetProcessLive($processHandle)
            if ($exitResult.Succeeded) {
                $script:EgState.launcher_exit_code = [int]$exitResult.ExitCode
            }
            else {
                Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_LAUNCHER_EXIT_READ_FAILED' `
                    -ErrorCode $exitResult.ErrorCode -ContainmentFailure
            }
        }
        elseif ($waitResult.Value -eq [EnergyGridOneShotSupervisorNative]::WAIT_FAILED) {
            Stop-EgSupervisor -SupportRef 'EG_SUPERVISOR_PROCESS_WAIT_FAILED' `
                -ErrorCode $waitResult.ErrorCode -ContainmentFailure
        }
    }

    if ($launcherSignaled -and -not $script:EgState.termination_started) {
        $graceDeadline = [int64]([System.Diagnostics.Stopwatch]::GetTimestamp() +
            ([int64]5 * [int64][System.Diagnostics.Stopwatch]::Frequency))
        while ($script:EgState.active_processes -gt 0 -and
            [System.Diagnostics.Stopwatch]::GetTimestamp() -lt $graceDeadline -and
            -not (Test-EgDeadlineReached -DeadlineTicks $deadlineTicks)) {
            if (-not $script:EgState.application_child_observed) {
                if (Test-EgApplicationChild -JobHandle $jobHandle -LauncherHandle $processHandle `
                    -LauncherPid $creation.ProcessInfo.dwProcessId -StartTicks $creationStartTicks) {
                    $script:EgState.application_child_observed = $true
                }
            }
            $null = Get-EgAccounting -JobHandle $jobHandle
            Start-Sleep -Milliseconds 100
        }
        if ($script:EgState.active_processes -gt 0) {
            $script:EgState.descendant_grace_expired = $true
            Invoke-EgTerminateJob -JobHandle $jobHandle -Reason 'DESCENDANT_GRACE_EXPIRED'
        }
    }
    if ($script:EgState.termination_started) {
        Wait-EgReap -JobHandle $jobHandle -DeadlineTicks $deadlineTicks -WindowSeconds 30
    }
    else {
        $finalAccounting = Get-EgAccounting -JobHandle $jobHandle
        $terminalAccountingReady = $true
        if ($finalAccounting.ActiveProcesses -eq 0) {
            $script:EgState.reap_confirmed = $true
        }
        else {
            Invoke-EgTerminateJob -JobHandle $jobHandle -Reason 'POST_CREATE_FAILURE'
            Wait-EgReap -JobHandle $jobHandle -DeadlineTicks $deadlineTicks -WindowSeconds 30
        }
    }
}
catch {
    if (-not $script:EgState.creation_attempted) {
        $script:EgState.precreate_rejection = $true
    }
    if ($_.Exception.PSObject.Properties['ErrorCode']) {
        $script:EgState.error_code = [int]$_.Exception.ErrorCode
    }
    if ($_.Exception.Message -like 'EG_SUPERVISOR_*') {
        $script:EgState.support_ref = $_.Exception.Message
    }
    elseif ($script:EgState.support_ref -eq 'EG_SUPERVISOR') {
        $script:EgState.support_ref = 'EG_SUPERVISOR_INFRASTRUCTURE_FAILURE'
    }
    if ($script:EgState.creation_succeeded) {
        $script:EgState.containment_failure = $true
        if (-not $script:EgState.termination_started -and $jobHandle -ne [IntPtr]::Zero) {
            Invoke-EgTerminateJob -JobHandle $jobHandle -Reason 'POST_CREATE_FAILURE'
        }
        if ($jobHandle -ne [IntPtr]::Zero) {
            Wait-EgReap -JobHandle $jobHandle -DeadlineTicks $deadlineTicks -WindowSeconds 30
        }
    }
}
finally {
    if ($null -ne $intentStream) {
        try { $intentStream.Dispose() } catch { $script:EgState.evidence_integrity_failure = $true }
        $intentStream = $null
    }
    if ($null -ne $attributeResources) {
        try { $attributeResources.Dispose() } catch { $script:EgState.containment_failure = $true }
        $attributeResources = $null
    }
    if ($null -ne $pipes) {
        Close-EgHandle -Handle $pipes.LauncherStdinRead
        Close-EgHandle -Handle $pipes.SupervisorStdinWrite
        Close-EgHandle -Handle $pipes.SupervisorStdoutRead
        Close-EgHandle -Handle $pipes.LauncherStdoutWrite
        Close-EgHandle -Handle $pipes.SupervisorStderrRead
        Close-EgHandle -Handle $pipes.LauncherStderrWrite
        $pipes = $null
    }
    if ($script:EgState.creation_succeeded -and $null -ne $stdoutDrain -and $null -ne $stderrDrain) {
        Wait-EgDrains -StdoutDrain $stdoutDrain -StderrDrain $stderrDrain
    }
    if ($threadHandle -ne [IntPtr]::Zero) {
        Close-EgHandle -Handle $threadHandle
        $threadHandle = [IntPtr]::Zero
    }
    if ($processHandle -ne [IntPtr]::Zero) {
        Close-EgHandle -Handle $processHandle
        $processHandle = [IntPtr]::Zero
    }
    if ($jobHandle -ne [IntPtr]::Zero) {
        Close-EgHandle -Handle $jobHandle
        $jobHandle = [IntPtr]::Zero
    }
    $handlerError = 0
    try {
        if (-not [EnergyGridOneShotSupervisorNative]::UninstallConsoleHandler([ref]$handlerError)) {
            $script:EgState.containment_failure = $true
            $script:EgState.error_code = $handlerError
            $script:EgState.support_ref = 'EG_SUPERVISOR_CONSOLE_HANDLER_CLOSE_FAILED'
        }
    }
    catch { }
}

if ($evidenceReady -and -not $script:EgState.duplicate_run_id) {
    $script:EgState.start_verdict = Get-EgStartVerdict
    Write-EgOutcome -StartVerdict $script:EgState.start_verdict
    if (-not $script:EgState.outcome_committed) {
        $script:EgState.start_verdict = 'AMBIGUOUS'
    }
}
else {
    $script:EgState.start_verdict = 'AMBIGUOUS'
}

$exitCode = Get-EgExitCode
Write-EgPublicProjection -ReceiptAvailable ([bool]$script:EgState.outcome_committed)
exit $exitCode
