using Microsoft.Win32.SafeHandles;
using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public sealed class EnergyGridDelayedFlushStreamForTest : FileStream
{
    private readonly int delayMilliseconds;

    public EnergyGridDelayedFlushStreamForTest(string path, int delayMilliseconds)
        : base(path, FileMode.Create, FileAccess.ReadWrite, FileShare.None, 4096,
            FileOptions.DeleteOnClose)
    {
        this.delayMilliseconds = delayMilliseconds;
    }

    public override void Flush(bool flushToDisk)
    {
        Thread.Sleep(delayMilliseconds);
        base.Flush(flushToDisk);
    }
}

public static class EnergyGridConcurrentSaturationWriters
{
    public static void Run(int byteCount)
    {
        byte[] payload = new byte[byteCount];
        ManualResetEvent startGate = new ManualResetEvent(false);
        object errorLock = new object();
        Exception failure = null;
        Thread stdoutWriter = new Thread(delegate()
        {
            try
            {
                startGate.WaitOne();
                using (Stream stream = Console.OpenStandardOutput())
                {
                    stream.Write(payload, 0, payload.Length);
                    stream.Flush();
                }
            }
            catch (Exception error)
            {
                lock (errorLock) { if (failure == null) { failure = error; } }
            }
        });
        Thread stderrWriter = new Thread(delegate()
        {
            try
            {
                startGate.WaitOne();
                using (Stream stream = Console.OpenStandardError())
                {
                    stream.Write(payload, 0, payload.Length);
                    stream.Flush();
                }
            }
            catch (Exception error)
            {
                lock (errorLock) { if (failure == null) { failure = error; } }
            }
        });
        stdoutWriter.Start();
        stderrWriter.Start();
        startGate.Set();
        bool stdoutFinished = stdoutWriter.Join(30000);
        bool stderrFinished = stderrWriter.Join(30000);
        startGate.Dispose();
        if (!stdoutFinished || !stderrFinished) { throw new TimeoutException(); }
        if (failure != null) { throw failure; }
    }
}

public enum FILE_INFO_BY_HANDLE_CLASS : int
{
    FileIdInfo = 18
}

[StructLayout(LayoutKind.Sequential)]
public struct FILE_ID_128
{
    public ulong Part0;
    public ulong Part1;
}

[StructLayout(LayoutKind.Sequential)]
public struct FILE_ID_INFO
{
    public ulong VolumeSerialNumber;
    public FILE_ID_128 FileId;
}

[StructLayout(LayoutKind.Sequential)]
public struct BY_HANDLE_FILE_INFORMATION
{
    public uint FileAttributes;
    public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
    public uint VolumeSerialNumber;
    public uint FileSizeHigh;
    public uint FileSizeLow;
    public uint NumberOfLinks;
    public uint FileIndexHigh;
    public uint FileIndexLow;
}

public sealed class EgIdentityOpenResult
{
    public bool Succeeded;
    public int ErrorCode;
    public SafeFileHandle Handle;
}

public sealed class EgIdentityInfoResult
{
    public bool Succeeded;
    public int ErrorCode;
    public ulong VolumeSerialNumber;
    public ulong FileIdPart0;
    public ulong FileIdPart1;
    public uint FileAttributes;
    public uint NumberOfLinks;
}

public sealed class EgIdentityReadResult
{
    public bool Succeeded;
    public int ErrorCode;
    public long InitialLength;
    public long FinalLength;
    public bool Eof;
    public byte[] Bytes;
}

public sealed class EgIdentityBooleanResult
{
    public bool Succeeded;
    public int ErrorCode;
}

public sealed class EgShortPathResult
{
    public bool Succeeded;
    public int ErrorCode;
    public string Path;
}

public static class EgOutcomeIdentity
{
    public const uint DirectoryAccess = 0x00100081;
    public const uint DirectoryShare = 0x00000003;
    public const uint DirectoryCreation = 0x00000003;
    public const uint DirectoryFlags = 0x02200000;
    public const uint FileAccess = 0x80100080;
    public const uint FileShare = 0x00000001;
    public const uint FileCreation = 0x00000003;
    public const uint FileFlags = 0x00200000;
    public const uint FileAttributeDirectory = 0x00000010;
    public const uint FileAttributeReparsePoint = 0x00000400;
    public const int FileIdInfoValue = 18;

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode,
        SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandleEx(
        SafeFileHandle hFile,
        FILE_INFO_BY_HANDLE_CLASS fileInformationClass,
        out FILE_ID_INFO fileInformation,
        uint bufferSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle hFile,
        out BY_HANDLE_FILE_INFORMATION fileInformation);

    [DllImport("kernel32.dll", EntryPoint = "GetShortPathNameW", CharSet = CharSet.Unicode,
        SetLastError = true)]
    private static extern uint GetShortPathNameW(
        string longPath,
        StringBuilder shortPath,
        int shortPathCapacity);

    [DllImport("kernel32.dll", EntryPoint = "CreateHardLinkW", CharSet = CharSet.Unicode,
        SetLastError = true)]
    private static extern bool CreateHardLinkW(
        string fileName,
        string existingFileName,
        IntPtr securityAttributes);

    public static bool VerifyLayout()
    {
        return Marshal.SizeOf(typeof(FILE_ID_128)) == 16 &&
            Marshal.SizeOf(typeof(FILE_ID_INFO)) == 24 &&
            Marshal.SizeOf(typeof(BY_HANDLE_FILE_INFORMATION)) == 52 &&
            (int)FILE_INFO_BY_HANDLE_CLASS.FileIdInfo == FileIdInfoValue;
    }

    private static EgIdentityOpenResult Open(
        string path, uint access, uint share, uint creation, uint flags)
    {
        EgIdentityOpenResult result = new EgIdentityOpenResult();
        result.Handle = CreateFileW(path, access, share, IntPtr.Zero, creation, flags, IntPtr.Zero);
        if (result.Handle == null || result.Handle.IsInvalid)
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
            return result;
        }
        result.Succeeded = true;
        return result;
    }

    public static EgIdentityOpenResult OpenDirectory(string path)
    {
        return Open(path, DirectoryAccess, DirectoryShare, DirectoryCreation, DirectoryFlags);
    }

    public static EgIdentityOpenResult OpenFile(string path)
    {
        return Open(path, FileAccess, FileShare, FileCreation, FileFlags);
    }

    public static EgIdentityInfoResult QueryInfo(SafeFileHandle handle)
    {
        EgIdentityInfoResult result = new EgIdentityInfoResult();
        if (handle == null || handle.IsInvalid)
        {
            result.ErrorCode = 6;
            return result;
        }

        FILE_ID_INFO identity;
        if (!GetFileInformationByHandleEx(
            handle,
            FILE_INFO_BY_HANDLE_CLASS.FileIdInfo,
            out identity,
            (uint)Marshal.SizeOf(typeof(FILE_ID_INFO))))
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
            return result;
        }

        BY_HANDLE_FILE_INFORMATION legacy;
        if (!GetFileInformationByHandle(handle, out legacy))
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
            return result;
        }

        result.Succeeded = true;
        result.VolumeSerialNumber = identity.VolumeSerialNumber;
        result.FileIdPart0 = identity.FileId.Part0;
        result.FileIdPart1 = identity.FileId.Part1;
        result.FileAttributes = legacy.FileAttributes;
        result.NumberOfLinks = legacy.NumberOfLinks;
        return result;
    }

    public static EgIdentityReadResult ReadRetained(SafeFileHandle original)
    {
        EgIdentityReadResult result = new EgIdentityReadResult();
        bool addedReference = false;
        try
        {
            if (original == null || original.IsInvalid)
            {
                result.ErrorCode = 6;
                return result;
            }

            original.DangerousAddRef(ref addedReference);
            using (SafeFileHandle borrowed = new SafeFileHandle(
                original.DangerousGetHandle(), false))
            using (FileStream stream = new FileStream(
                borrowed, System.IO.FileAccess.Read, 65536, false))
            {
                result.InitialLength = stream.Length;
                if (result.InitialLength < 1 || result.InitialLength > 65536)
                {
                    return result;
                }

                byte[] bytes = new byte[(int)result.InitialLength];
                int offset = 0;
                while (offset < bytes.Length)
                {
                    int read = stream.Read(bytes, offset, bytes.Length - offset);
                    if (read <= 0)
                    {
                        return result;
                    }
                    offset += read;
                }

                result.Eof = stream.ReadByte() == -1;
                result.FinalLength = stream.Length;
                result.Bytes = bytes;
                result.Succeeded = result.Eof && result.FinalLength == result.InitialLength;
                if (!result.Succeeded)
                {
                    Array.Clear(bytes, 0, bytes.Length);
                    result.Bytes = null;
                }
            }
        }
        catch
        {
            result.Succeeded = false;
            result.Bytes = null;
        }
        finally
        {
            if (addedReference)
            {
                original.DangerousRelease();
            }
        }
        return result;
    }

    public static EgIdentityBooleanResult CreateHardLink(string linkPath, string existingPath)
    {
        EgIdentityBooleanResult result = new EgIdentityBooleanResult();
        result.Succeeded = CreateHardLinkW(linkPath, existingPath, IntPtr.Zero);
        if (!result.Succeeded)
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
        }
        return result;
    }

    public static EgShortPathResult GetShortPath(string path)
    {
        EgShortPathResult result = new EgShortPathResult();
        int capacity = 260;
        while (capacity <= 32768)
        {
            StringBuilder buffer = new StringBuilder(capacity);
            uint length = GetShortPathNameW(path, buffer, buffer.Capacity);
            if (length == 0)
            {
                result.ErrorCode = Marshal.GetLastWin32Error();
                return result;
            }
            if (length < (uint)buffer.Capacity)
            {
                result.Succeeded = true;
                result.Path = buffer.ToString();
                return result;
            }
            capacity = checked((int)length + 1);
        }
        result.ErrorCode = 122;
        return result;
    }
}

public sealed class EnergyGridDelayedFlushStreamForFunctionTest : FileStream
{
    private readonly int delayMilliseconds;

    public EnergyGridDelayedFlushStreamForFunctionTest(string path, int delayMilliseconds)
        : base(path, FileMode.Create, FileAccess.ReadWrite, FileShare.None, 4096,
            FileOptions.DeleteOnClose)
    {
        this.delayMilliseconds = delayMilliseconds;
    }

    public override void Flush(bool flushToDisk)
    {
        Thread.Sleep(delayMilliseconds);
        base.Flush(flushToDisk);
    }
}

public sealed class EnergyGridOutcomeDelayedFlushStreamForFunctionTest : FileStream
{
    private const int DelayMilliseconds = 6500;
    private static readonly object workerLock = new object();

    public static readonly ManualResetEvent FlushEntered = new ManualResetEvent(false);
    public static readonly ManualResetEvent ReleaseFlush = new ManualResetEvent(false);
    public static Thread WorkerThread;

    public EnergyGridOutcomeDelayedFlushStreamForFunctionTest(
        string path, FileMode mode, FileAccess access, FileShare share,
        int bufferSize, FileOptions options)
        : base(path, mode, access, share, bufferSize, options)
    {
    }

    public static void ResetState()
    {
        FlushEntered.Reset();
        ReleaseFlush.Reset();
        lock (workerLock) { WorkerThread = null; }
    }

    public static bool IsReleaseClosed()
    {
        return !ReleaseFlush.WaitOne(0);
    }

    public static void Release()
    {
        ReleaseFlush.Set();
    }

    public static bool JoinWorker(int timeoutMilliseconds)
    {
        Thread worker;
        lock (workerLock) { worker = WorkerThread; }
        return worker != null && worker.Join(timeoutMilliseconds);
    }

    public override void Flush(bool flushToDisk)
    {
        Thread currentThread = Thread.CurrentThread;
        if (!currentThread.IsBackground)
        {
            base.Flush(flushToDisk);
            return;
        }
        lock (workerLock)
        {
            if (WorkerThread != null && WorkerThread != currentThread)
            {
                base.Flush(flushToDisk);
                return;
            }
            WorkerThread = currentThread;
        }
        FlushEntered.Set();
        Thread.Sleep(DelayMilliseconds);
        if (!ReleaseFlush.WaitOne(120000))
        {
            throw new TimeoutException("test release event timed out");
        }
        base.Flush(flushToDisk);
    }
}

public static class EnergyGridGraceInterruptSchedulerForFunctionTest
{
    public static Thread Schedule(int delayMilliseconds)
    {
        Thread thread = new Thread(delegate()
        {
            Thread.Sleep(delayMilliseconds);
            Type nativeType = null;
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                nativeType = assembly.GetType("EnergyGridOneShotSupervisorNative");
                if (nativeType != null) { break; }
            }
            if (nativeType == null) { throw new InvalidOperationException("native type missing"); }
            MethodInfo signalMethod = nativeType.GetMethod(
                "HandleConsoleSignal", BindingFlags.NonPublic | BindingFlags.Static);
            signalMethod.Invoke(null, new object[] { (uint)2 });
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }
}

public sealed class EgN7ProcessMetadataResult
{
    public bool Succeeded;
    public bool TimedOut;
    public bool ProviderFailed;
    public int ErrorCode;
    public uint ParentProcessId;
    public string ImagePath;
    public string CommandLine;
}

public static class EnergyGridOneShotSupervisorN7ProviderControl
{
    private static readonly ManualResetEvent ProviderEntered = new ManualResetEvent(false);
    private static readonly ManualResetEvent ProviderRelease = new ManualResetEvent(false);
    private static readonly ManualResetEvent ProviderCompleted = new ManualResetEvent(false);
    private static int observerBusy;
    private static int providerCalls;
    private static int lastTimeoutMilliseconds;
    private static uint lastProcessId;
    private static int lateSuccess;

    public static void Reset()
    {
        ProviderEntered.Reset();
        ProviderRelease.Reset();
        ProviderCompleted.Reset();
        Interlocked.Exchange(ref observerBusy, 0);
        Interlocked.Exchange(ref providerCalls, 0);
        Interlocked.Exchange(ref lastTimeoutMilliseconds, 0);
        lastProcessId = 0;
        Interlocked.Exchange(ref lateSuccess, 0);
    }

    public static void SetBusy()
    {
        Interlocked.Exchange(ref observerBusy, 1);
    }

    public static void SetLateSuccess(bool enabled)
    {
        Interlocked.Exchange(ref lateSuccess, enabled ? 1 : 0);
    }

    public static EgN7ProcessMetadataResult QueryProcessMetadata(uint processId, int timeoutMilliseconds)
    {
        EgN7ProcessMetadataResult result = new EgN7ProcessMetadataResult();
        lastProcessId = processId;
        Interlocked.Exchange(ref lastTimeoutMilliseconds, timeoutMilliseconds);
        if (timeoutMilliseconds <= 0 || Interlocked.CompareExchange(ref observerBusy, 1, 0) != 0)
        {
            result.ProviderFailed = true;
            return result;
        }
        Exception workerFailure = null;
        Thread queryThread = new Thread(delegate()
        {
            try
            {
                Interlocked.Increment(ref providerCalls);
                ProviderEntered.Set();
                ProviderRelease.WaitOne();
                if (Interlocked.CompareExchange(ref lateSuccess, 0, 0) != 0)
                {
                    result.ParentProcessId = 1;
                    result.CommandLine = "late-provider-success";
                    result.Succeeded = true;
                }
            }
            catch (Exception ex)
            {
                workerFailure = ex;
            }
            finally
            {
                ProviderCompleted.Set();
                Interlocked.Exchange(ref observerBusy, 0);
            }
        });
        queryThread.IsBackground = true;
        queryThread.Start();
        if (!queryThread.Join(timeoutMilliseconds))
        {
            result.TimedOut = true;
        }
        else if (workerFailure != null)
        {
            result.ProviderFailed = true;
        }
        return result;
    }

    public static bool WaitUntilEntered(int milliseconds) { return ProviderEntered.WaitOne(milliseconds); }
    public static bool IsCompleted() { return ProviderCompleted.WaitOne(0); }
    public static bool WaitUntilCompleted(int milliseconds) { return ProviderCompleted.WaitOne(milliseconds); }
    public static void Release() { ProviderRelease.Set(); }
    public static int GetCallCount() { return Interlocked.CompareExchange(ref providerCalls, 0, 0); }
    public static int GetTimeoutMilliseconds() { return Interlocked.CompareExchange(ref lastTimeoutMilliseconds, 0, 0); }
    public static uint GetProcessId() { return lastProcessId; }
    public static bool IsBusy() { return Interlocked.CompareExchange(ref observerBusy, 0, 0) != 0; }
}

public sealed class EgHandleCanaryResult
{
    public bool Succeeded;
    public int ErrorCode;
    public uint VolumeSerialNumber;
    public ulong FileIndex;
}

public static class EnergyGridHandleCanaryIdentity
{
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle handle, out BY_HANDLE_FILE_INFORMATION information);

    public static EgHandleCanaryResult Query(SafeFileHandle handle)
    {
        EgHandleCanaryResult result = new EgHandleCanaryResult();
        BY_HANDLE_FILE_INFORMATION information;
        if (handle == null || handle.IsInvalid ||
            !GetFileInformationByHandle(handle, out information))
        {
            result.ErrorCode = Marshal.GetLastWin32Error();
            return result;
        }
        result.Succeeded = true;
        result.VolumeSerialNumber = information.VolumeSerialNumber;
        result.FileIndex = ((ulong)information.FileIndexHigh << 32) | information.FileIndexLow;
        return result;
    }
}
