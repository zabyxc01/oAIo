package com.oaio.kira

import android.util.Base64
import android.util.Log
import kotlinx.coroutines.*
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.util.concurrent.TimeUnit

/**
 * WebSocket client for the oAIo companion protocol.
 * Handles connection, messaging, audio encoding/decoding, and keepalive.
 */
class CompanionClient(
    private val listener: Listener
) {
    interface Listener {
        fun onConnected()
        fun onDisconnected()
        fun onChatResponse(text: String, emotion: JSONObject?, done: Boolean)
        fun onTtsAudio(audioData: ByteArray, format: String, sampleRate: Int)
        fun onSttTranscript(text: String)
        fun onStateSync(config: JSONObject)
    }

    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(15, TimeUnit.SECONDS)
        .build()

    private var ws: WebSocket? = null
    private var scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var connected = false
    private var retryDelay = 1000L

    fun connect(host: String) {
        val url = "ws://$host/extensions/companion/ws"
        Log.i(TAG, "Connecting to $url")
        val request = Request.Builder().url(url).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                connected = true
                retryDelay = 1000L
                Log.i(TAG, "Connected")
                listener.onConnected()
                // Send state sync
                send(JSONObject().apply {
                    put("type", "state.sync")
                    put("id", uid())
                    put("ts", System.currentTimeMillis() / 1000.0)
                    put("payload", JSONObject().apply {
                        put("client_type", "android")
                        put("platform", "android")
                        put("name", "Kira Phone")
                        put("capabilities", JSONArray().apply {
                            put("chat"); put("tts"); put("stt")
                        })
                    })
                })
            }

            override fun onMessage(ws: WebSocket, text: String) {
                try {
                    val msg = JSONObject(text)
                    handleMessage(msg)
                } catch (e: Exception) {
                    Log.e(TAG, "Parse error: ${e.message}")
                }
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                connected = false
                listener.onDisconnected()
                scheduleReconnect(host)
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                connected = false
                Log.e(TAG, "Connection failed: ${t.message}")
                listener.onDisconnected()
                scheduleReconnect(host)
            }
        })
    }

    fun disconnect() {
        ws?.close(1000, "Client closing")
        ws = null
        connected = false
        scope.cancel()
    }

    fun sendChat(text: String, history: List<JSONObject> = emptyList()) {
        val historyArray = JSONArray().apply {
            history.forEach { put(it) }
        }
        send(JSONObject().apply {
            put("type", "chat.request")
            put("id", uid())
            put("ts", System.currentTimeMillis() / 1000.0)
            put("payload", JSONObject().apply {
                put("text", text)
                put("history", historyArray)
            })
        })
    }

    fun sendAudio(pcmData: ByteArray, sampleRate: Int, history: List<JSONObject> = emptyList()) {
        val wav = pcmToWav(pcmData, sampleRate)
        val b64 = Base64.encodeToString(wav, Base64.NO_WRAP)
        val historyArray = JSONArray().apply {
            history.forEach { put(it) }
        }
        send(JSONObject().apply {
            put("type", "stt.audio")
            put("id", uid())
            put("ts", System.currentTimeMillis() / 1000.0)
            put("payload", JSONObject().apply {
                put("audio_b64", b64)
                put("format", "wav")
                put("sample_rate", sampleRate)
                put("auto_chat", true)
                put("history", historyArray)
            })
        })
    }

    private fun handleMessage(msg: JSONObject) {
        val type = msg.optString("type", "")
        val payload = msg.optJSONObject("payload") ?: return

        when (type) {
            "chat.response" -> {
                val text = payload.optString("text", "")
                val done = payload.optBoolean("done", true)
                val emotion = payload.optJSONObject("emotion")
                listener.onChatResponse(text, emotion, done)
            }
            "tts.audio" -> {
                val b64 = payload.optString("audio_b64", "")
                val format = payload.optString("format", "wav")
                val sr = payload.optInt("sample_rate", 24000)
                if (b64.isNotEmpty()) {
                    val data = Base64.decode(b64, Base64.DEFAULT)
                    listener.onTtsAudio(data, format, sr)
                }
            }
            "stt.transcript" -> {
                listener.onSttTranscript(payload.optString("text", ""))
            }
            "state.sync" -> {
                listener.onStateSync(payload)
            }
            "pong" -> { /* keepalive ack */ }
        }
    }

    private fun send(json: JSONObject) {
        if (connected) ws?.send(json.toString())
    }

    private fun scheduleReconnect(host: String) {
        scope.launch {
            delay(retryDelay)
            retryDelay = (retryDelay * 1.5).toLong().coerceAtMost(30000)
            connect(host)
        }
    }

    private fun uid(): String = java.util.UUID.randomUUID().toString().take(12)

    companion object {
        private const val TAG = "CompanionClient"

        /** Convert raw PCM (mono 16-bit) to WAV format */
        fun pcmToWav(pcm: ByteArray, sampleRate: Int): ByteArray {
            val out = ByteArrayOutputStream()
            val dos = DataOutputStream(out)
            val channels = 1
            val bitsPerSample = 16
            val byteRate = sampleRate * channels * bitsPerSample / 8
            val blockAlign = channels * bitsPerSample / 8

            // RIFF header
            dos.writeBytes("RIFF")
            dos.writeIntLE(36 + pcm.size)
            dos.writeBytes("WAVE")
            // fmt chunk
            dos.writeBytes("fmt ")
            dos.writeIntLE(16)
            dos.writeShortLE(1) // PCM
            dos.writeShortLE(channels)
            dos.writeIntLE(sampleRate)
            dos.writeIntLE(byteRate)
            dos.writeShortLE(blockAlign)
            dos.writeShortLE(bitsPerSample)
            // data chunk
            dos.writeBytes("data")
            dos.writeIntLE(pcm.size)
            dos.write(pcm)

            return out.toByteArray()
        }

        private fun DataOutputStream.writeIntLE(v: Int) {
            write(v and 0xFF); write((v shr 8) and 0xFF)
            write((v shr 16) and 0xFF); write((v shr 24) and 0xFF)
        }
        private fun DataOutputStream.writeShortLE(v: Int) {
            write(v and 0xFF); write((v shr 8) and 0xFF)
        }
    }
}
