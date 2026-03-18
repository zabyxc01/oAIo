package com.oaio.kira

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.IBinder
import android.view.inputmethod.EditorInfo
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    private lateinit var hostInput: EditText
    private lateinit var connectBtn: Button
    private lateinit var statusDot: TextView
    private lateinit var chatLog: TextView
    private lateinit var chatScroll: ScrollView
    private lateinit var textInput: EditText
    private lateinit var sendBtn: Button
    private lateinit var micBtn: Button

    private var client: CompanionClient? = null
    private var audio: AudioManager? = null
    private var isRecording = false
    private var connected = false

    private val chatHistory = mutableListOf<JSONObject>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        hostInput = findViewById(R.id.host_input)
        connectBtn = findViewById(R.id.connect_btn)
        statusDot = findViewById(R.id.status_dot)
        chatLog = findViewById(R.id.chat_log)
        chatScroll = findViewById(R.id.chat_scroll)
        textInput = findViewById(R.id.text_input)
        sendBtn = findViewById(R.id.send_btn)
        micBtn = findViewById(R.id.mic_btn)

        audio = AudioManager()

        // Load saved host
        val prefs = getSharedPreferences("kira_prefs", MODE_PRIVATE)
        hostInput.setText(prefs.getString("host", KiraService.DEFAULT_HOST))

        // Request mic permission
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.RECORD_AUDIO), 1)
        }

        connectBtn.setOnClickListener {
            val host = hostInput.text.toString().trim()
            if (host.isEmpty()) return@setOnClickListener
            prefs.edit().putString("host", host).apply()
            connect(host)
        }

        sendBtn.setOnClickListener { sendMessage() }
        textInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) { sendMessage(); true } else false
        }

        micBtn.setOnClickListener {
            if (isRecording) {
                isRecording = false
                micBtn.text = "\uD83C\uDF99"
                val pcm = audio?.stopCapture() ?: return@setOnClickListener
                if (pcm.size >= AudioManager.SAMPLE_RATE) {
                    appendLog("You", "[voice]")
                    client?.sendAudio(pcm, AudioManager.SAMPLE_RATE, chatHistory.map { it })
                }
            } else {
                isRecording = true
                micBtn.text = "\u23F9"
                audio?.startCapture()
            }
        }

        // Auto-connect on launch
        val host = hostInput.text.toString().trim()
        if (host.isNotEmpty()) connect(host)
    }

    private fun connect(host: String) {
        client?.disconnect()
        appendLog("System", "Connecting to $host...")

        client = CompanionClient(object : CompanionClient.Listener {
            override fun onConnected() {
                connected = true
                runOnUiThread {
                    statusDot.text = "\uD83D\uDFE2"
                    connectBtn.text = "Connected"
                    appendLog("System", "Connected to oAIo")
                }
            }

            override fun onDisconnected() {
                connected = false
                runOnUiThread {
                    statusDot.text = "\uD83D\uDD34"
                    connectBtn.text = "Connect"
                }
            }

            override fun onChatResponse(text: String, emotion: JSONObject?, done: Boolean) {
                if (done && text.isNotEmpty()) {
                    chatHistory.add(JSONObject().put("role", "assistant").put("content", text))
                    runOnUiThread { appendLog("Kira", text) }
                }
            }

            override fun onTtsAudio(audioData: ByteArray, format: String, sampleRate: Int) {
                audio?.queueAudio(audioData, format)
            }

            override fun onSttTranscript(text: String) {
                if (text.isNotEmpty() && !text.startsWith("[")) {
                    chatHistory.add(JSONObject().put("role", "user").put("content", text))
                    runOnUiThread { appendLog("You", text) }
                }
            }

            override fun onStateSync(config: JSONObject) {}
        })
        client?.connect(host)
    }

    private fun sendMessage() {
        val text = textInput.text.toString().trim()
        if (text.isEmpty()) return
        textInput.text.clear()
        appendLog("You", text)
        chatHistory.add(JSONObject().put("role", "user").put("content", text))
        client?.sendChat(text, chatHistory.map { it })
    }

    private fun appendLog(sender: String, text: String) {
        chatLog.append("$sender: $text\n\n")
        chatScroll.post { chatScroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    override fun onDestroy() {
        client?.disconnect()
        audio?.release()
        super.onDestroy()
    }
}
