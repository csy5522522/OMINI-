"""Offline microphone dictation through the installed Windows recognizer."""
import os
import threading
from pathlib import Path


class SpeechInput:
    def __init__(self):
        self.lock = threading.RLock()
        self.engine = None
        self.timer = None
        self.active = False
        self.text = ''
        self.partial = ''
        self.error = ''
        self.language = ''

    def start(self, audio_path=None):
        with self.lock:
            if self.active:
                return self.status()
            if self.engine is not None:
                self.engine.Dispose()
                self.engine = None
            if os.name != 'nt':
                raise ValueError('语音输入使用 Windows 本地语音识别')
            try:
                import clr
                base = Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/assembly/GAC_MSIL/System.Speech'
                dll = next(base.rglob('System.Speech.dll'))
                clr.AddReference(str(dll))
                from System.Speech.Recognition import SpeechRecognitionEngine, DictationGrammar, RecognizeMode
                recognizers = list(SpeechRecognitionEngine.InstalledRecognizers())
                chinese = next((r for r in recognizers if str(r.Culture.Name).startswith('zh')), None)
                if chinese is None:
                    raise ValueError('请在 Windows 安装中文语音识别包后重试')
                self.engine = SpeechRecognitionEngine(chinese)
                self.language = str(chinese.Culture.Name)
                self.engine.LoadGrammar(DictationGrammar())
                if audio_path is None:
                    self.engine.SetInputToDefaultAudioDevice()
                else:
                    # Internal diagnostic input; the HTTP API always uses the microphone.
                    self.engine.SetInputToWaveFile(str(audio_path))
                self.engine.SpeechRecognized += self._recognized
                self.engine.SpeechHypothesized += self._hypothesis
                self.engine.RecognizeCompleted += self._completed
                self.text = self.partial = self.error = ''
                self.active = True
                self.engine.RecognizeAsync(RecognizeMode.Multiple)
                self.timer = threading.Timer(60,self.stop)
                self.timer.daemon = True
                self.timer.start()
                return self.status()
            except Exception as error:
                self.active = False
                if self.engine:
                    self.engine.Dispose()
                    self.engine = None
                raise ValueError('语音输入启动失败，请检查麦克风与 Windows 中文语音包：'+str(error).split('\n')[0]) from None

    def _recognized(self, _sender, event):
        with self.lock:
            if event.Result.Confidence >= .3:
                self.text += str(event.Result.Text)+'，'
            self.partial = ''

    def _hypothesis(self, _sender, event):
        with self.lock:
            self.partial = str(event.Result.Text)

    def _completed(self, _sender, event):
        with self.lock:
            self.active = False
            if self.timer:
                self.timer.cancel()
            if event.Error:
                self.error = str(event.Error)

    def status(self):
        with self.lock:
            return {'active':self.active,'text':self.text,'partial':self.partial,'error':self.error,'language':self.language}

    def stop(self):
        with self.lock:
            self.active = False
            self.partial = ''
            if self.timer:
                self.timer.cancel()
            engine, self.engine = self.engine, None
        # CLR completion callbacks acquire the same lock; cancel outside the lock.
        if engine:
            engine.RecognizeAsyncCancel()
            engine.Dispose()
        return self.status()
