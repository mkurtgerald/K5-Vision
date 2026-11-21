// Native QA only: compile with the actual production sources and select this
// Main. No application entry point, service, profile or ACL operation is used.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Reflection;
using System.Threading;

namespace K5VisionDesktop
{
    internal static class RecorderWireProbe
    {
        private sealed class Pair : IDisposable
        {
            private readonly string name = "K5WireProbe-" + Guid.NewGuid().ToString("N");
            internal readonly NamedPipeServerStream Server;
            internal NamedPipeClientStream Client;

            internal Pair()
            {
                Server = new NamedPipeServerStream(name, PipeDirection.InOut, 1,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 4096, 4096);
                try { Connect(); }
                catch { Dispose(); throw; }
            }

            private void Connect()
            {
                IAsyncResult waiting = Server.BeginWaitForConnection(null, null);
                Client = new NamedPipeClientStream(".", name, PipeDirection.InOut, PipeOptions.Asynchronous);
                Client.Connect(2000);
                // EndWaitForConnection also owns its APM resources.
                Server.EndWaitForConnection(waiting);
            }

            internal void Reconnect()
            {
                IntPtr expected = Server.SafePipeHandle.DangerousGetHandle();
                Client.Dispose();
                Server.Disconnect(); // Also resets the Broken state after EOF.
                Connect();
                Require(Server.SafePipeHandle.DangerousGetHandle() == expected);
            }

            public void Dispose()
            {
                if (Client != null) Client.Dispose();
                if (Server != null) Server.Dispose();
            }
        }

        private static void Require(bool condition)
        {
            if (!condition) throw new InvalidOperationException();
        }

        private static Dictionary<string, object> Message(string operation)
        {
            return new Dictionary<string, object> { { "schema_version", 1 }, { "operation", operation } };
        }

        private static void Roundtrip(Pair pair)
        {
            using (ManualResetEvent cancel = new ManualResetEvent(false))
            {
                RecorderWire.Write(pair.Client, Message("status"), 512, 2000, cancel);
                Dictionary<string, object> first = RecorderWire.Read(pair.Server, 512, 2000, cancel);
                Require(RecorderWire.Operation(first) == "status");
                RecorderWire.Write(pair.Server, Message("reply"), 512, 2000, cancel);
                Dictionary<string, object> reply = RecorderWire.Read(pair.Client, 512, 2000, cancel);
                Require(RecorderWire.Operation(reply) == "reply");
            }
        }

        private static void RefuseRead(Pair pair, ManualResetEvent cancel, int budget)
        {
            Stopwatch elapsed = Stopwatch.StartNew();
            bool refused = false;
            try { RecorderWire.Read(pair.Server, 512, budget, cancel); }
            catch (InvalidOperationException error)
            {
                if (error is ObjectDisposedException) throw;
                refused = true;
            }
            Require(refused && elapsed.ElapsedMilliseconds < 2500);
        }

        private static void AwaitPeerClose(Pair pair, ManualResetEvent cancel, int budget)
        {
            // Reflection keeps this common fixture compilable with immutable
            // baseline sources that predate the production close-wait helper.
            MethodInfo method = typeof(RecorderWire).GetMethod("AwaitPeerClose",
                BindingFlags.Static | BindingFlags.NonPublic);
            if (method == null) throw new MissingMethodException();
            try { method.Invoke(null, new object[] { pair.Server, budget, cancel }); }
            catch (TargetInvocationException error) { throw error.InnerException; }
        }

        private static bool DisconnectDiscardsUnreadReply()
        {
            using (Pair pair = new Pair())
            using (ManualResetEvent cancel = new ManualResetEvent(false))
            {
                RecorderWire.Write(pair.Server, Message("reply"), 512, 2000, cancel);
                pair.Server.Disconnect();
                Thread.Sleep(120); // No client read is issued before disconnect.
                try { RecorderWire.Read(pair.Client, 512, 1000, cancel); }
                catch (ObjectDisposedException) { throw; }
                catch (IOException) { return true; }
                catch (InvalidOperationException) { return true; }
                return false;
            }
        }

        private static void DelayedReaderReceivesReply()
        {
            using (Pair pair = new Pair())
            using (ManualResetEvent cancel = new ManualResetEvent(false))
            using (ManualResetEvent waiting = new ManualResetEvent(false))
            {
                Exception failure = null;
                RecorderWire.Write(pair.Server, Message("reply"), 512, 2000, cancel);
                Thread closer = new Thread(delegate() {
                    waiting.Set();
                    try { AwaitPeerClose(pair, cancel, 2000); pair.Server.Disconnect(); }
                    catch (Exception error) { failure = error; }
                }) { IsBackground = true };
                closer.Start();
                try
                {
                    Require(waiting.WaitOne(1000));
                    Thread.Sleep(120);
                    Dictionary<string, object> response = RecorderWire.Read(pair.Client, 512, 1000, cancel);
                    Require(RecorderWire.Operation(response) == "reply");
                }
                finally
                {
                    pair.Client.Dispose(); // The actual client also closes after reading.
                    Require(closer.Join(4000));
                }
                if (failure != null) throw failure;
            }
        }

        private static void NonclosingPeerIsBounded()
        {
            using (Pair pair = new Pair())
            using (ManualResetEvent cancel = new ManualResetEvent(false))
            {
                RecorderWire.Write(pair.Server, Message("reply"), 512, 2000, cancel);
                Require(RecorderWire.Operation(RecorderWire.Read(pair.Client, 512, 1000, cancel)) == "reply");
                Stopwatch elapsed = Stopwatch.StartNew();
                bool refused = false;
                try { AwaitPeerClose(pair, cancel, 120); }
                catch (InvalidOperationException error)
                {
                    if (error is ObjectDisposedException) throw;
                    refused = true;
                }
                Require(refused && elapsed.ElapsedMilliseconds < 2500);
                pair.Reconnect(); Roundtrip(pair);
            }
        }

        private static int Main(string[] arguments)
        {
            Dictionary<string, object> receipt = new Dictionary<string, object> { { "schema_version", 1 } };
            try
            {
                Require(arguments.Length == 1 && (arguments[0] == "--baseline" ||
                    arguments[0] == "--delivery-baseline" || arguments[0] == "--fixed"));
                if (arguments[0] == "--delivery-baseline")
                {
                    Require(DisconnectDiscardsUnreadReply());
                    receipt["status"] = "delivery_baseline_reproduced";
                    receipt["failure_type"] = "UnreadResponseDiscarded";
                    receipt["disconnect_discards_unread_reply"] = true;
                    Console.WriteLine(Json.Serializer.Serialize(receipt));
                    return 11;
                }
                using (Pair pair = new Pair())
                {
                    if (arguments[0] == "--baseline")
                    {
                        try { Roundtrip(pair); }
                        catch (ObjectDisposedException)
                        {
                            receipt["status"] = "baseline_reproduced";
                            receipt["failure_type"] = "ObjectDisposedException";
                            Console.WriteLine(Json.Serializer.Serialize(receipt));
                            return 10;
                        }
                        throw new InvalidOperationException();
                    }

                    Roundtrip(pair);
                    receipt["roundtrip"] = true;
                    using (ManualResetEvent cancel = new ManualResetEvent(false))
                    {
                        // A zero frame length must be refused after its real
                        // header read; the same server instance remains usable.
                        pair.Client.Write(new byte[4], 0, 4);
                        RefuseRead(pair, cancel, 1000);
                    }
                    pair.Reconnect(); Roundtrip(pair);
                    receipt["malformed_length_refused"] = true;

                    using (ManualResetEvent cancel = new ManualResetEvent(false))
                        RefuseRead(pair, cancel, 120);
                    pair.Reconnect(); Roundtrip(pair);
                    receipt["timeout_refused"] = true;

                    using (ManualResetEvent cancel = new ManualResetEvent(false))
                    using (Timer signal = new Timer(delegate { cancel.Set(); }, null, 40, Timeout.Infinite))
                        RefuseRead(pair, cancel, 2000);
                    pair.Reconnect(); Roundtrip(pair);
                    receipt["cancellation_refused"] = true;
                    receipt["same_instance_reconnect"] = true;
                }
                DelayedReaderReceivesReply();
                receipt["delayed_reader_response_preserved"] = true;
                NonclosingPeerIsBounded();
                receipt["nonclosing_peer_bounded"] = true;
                receipt["status"] = "passed";
                Console.WriteLine(Json.Serializer.Serialize(receipt));
                return 0;
            }
            catch (Exception error)
            {
                receipt["status"] = "failed";
                // Exception types only; never supplied paths, names or messages.
                receipt["failure_type"] = error.GetType().Name;
                Console.WriteLine(Json.Serializer.Serialize(receipt));
                return 1;
            }
        }
    }
}
