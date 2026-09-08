using System.Buffers.Binary;

namespace Codito.Broker;

internal enum PeSubsystem : ushort
{
    Unknown = 0,
    WindowsGui = 2,
    WindowsConsole = 3,
}

internal static class PortableExecutable
{
    internal static PeSubsystem ReadSubsystem(string path)
    {
        try
        {
            using var stream = new FileStream(
                path, FileMode.Open, FileAccess.Read, FileShare.Read, 4096, FileOptions.RandomAccess);
            Span<byte> dos = stackalloc byte[64];
            stream.ReadExactly(dos);
            if (dos[0] != (byte)'M' || dos[1] != (byte)'Z')
            {
                return PeSubsystem.Unknown;
            }
            var peOffset = BinaryPrimitives.ReadInt32LittleEndian(dos[0x3c..]);
            if (peOffset < 64 || peOffset > stream.Length - 96)
            {
                return PeSubsystem.Unknown;
            }
            stream.Position = peOffset;
            Span<byte> headers = stackalloc byte[96];
            stream.ReadExactly(headers);
            if (!headers[..4].SequenceEqual("PE\0\0"u8))
            {
                return PeSubsystem.Unknown;
            }
            var magic = BinaryPrimitives.ReadUInt16LittleEndian(headers[24..]);
            if (magic is not (0x10b or 0x20b))
            {
                return PeSubsystem.Unknown;
            }
            return (PeSubsystem)BinaryPrimitives.ReadUInt16LittleEndian(headers[92..]);
        }
        catch (IOException)
        {
            return PeSubsystem.Unknown;
        }
    }
}
