import json
import requests
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor import discover, get, load_sources, run, telegram_post, send_link


class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "prueba", "name": "Prueba", "url": "https://example.org/listado/",
            "selector": "main a[href]", "extensions": [".pdf"], "enabled": True,
        }

    def test_filtro_y_enlaces_relativos(self):
        html = '<a href="/fuera.pdf">Fuera</a><main><a href="../uno.pdf">Uno</a><a href="otro">No</a></main>'
        self.assertEqual([v["url"] for v in discover(self.source, html).values()],
                         ["https://example.org/uno.pdf"])

    def test_type_application_y_aria_label(self):
        html = ('<main><a href="/archivo/123" type="application/pdf" '
                'aria-label="Resolución de prueba"></a>'
                '<a href="/pagina" type="application/xhtml+xml">No es documento</a></main>')
        found = list(discover(self.source, html).values())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], "Resolución de prueba")

    def test_atributo_download_y_marcadores(self):
        html = ('<main><a href="/fichero/123" download>Adjunto</a>'
                '<a href="/descargas/456">Resolución</a></main>')
        source = {**self.source, "download_markers": ["/descargas/"]}
        self.assertEqual(len(discover(source, html)), 2)

    def test_extension_en_query(self):
        html = '<main><a href="/archivo?id=123&nombre=acta.pdf">Acta</a></main>'
        self.assertEqual(len(discover(self.source, html)), 1)

    def test_deduplicacion(self):
        html = '<main><a href="/a.pdf">A</a><a href="/a.pdf">A</a></main>'
        self.assertEqual(len(discover(self.source, html)), 1)

    def test_id_por_fuente(self):
        html = '<main><a href="/a.pdf">A</a></main>'
        self.assertNotEqual(list(discover(self.source, html)),
                            list(discover({**self.source, "id": "otra"}, html)))

    def test_inventario_y_envio_posterior(self):
        first = discover(self.source, '<main><a href="/a.pdf">A</a></main>')
        second = discover(self.source, '<main><a href="/a.pdf">A</a><a href="/b.pdf">B</a></main>')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch("monitor.discover", side_effect=[first, second]), patch("monitor.send_document") as send:
                self.assertEqual(run([self.source], state), [])
                send.assert_not_called()
                self.assertEqual(run([self.source], state, token="test", default_chat="-100"), [])
                send.assert_called_once()
            self.assertEqual(len(json.loads(state.read_text())["prueba"]["processed"]), 2)

    def test_cambio_de_url_crea_nuevo_inventario_sin_enviar(self):
        first = discover(self.source, '<main><a href="/a.pdf">A</a></main>')
        other = {**self.source, "url": "https://otra.example.org/documentos"}
        changed = discover(other, '<main><a href="/nuevo.pdf">Nuevo</a></main>')
        later = discover(other, '<main><a href="/nuevo.pdf">Nuevo</a><a href="/posterior.pdf">Posterior</a></main>')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch("monitor.discover", side_effect=[first, changed, later]), patch("monitor.send_document") as send:
                self.assertEqual(run([self.source], state), [])
                self.assertEqual(run([other], state, token="test", default_chat="-100"), [])
                send.assert_not_called()
                data = json.loads(state.read_text())["prueba"]
                self.assertEqual(data["url"], other["url"])
                self.assertEqual(len(data["processed"]), 1)
                self.assertEqual(run([other], state, token="test", default_chat="-100"), [])
                send.assert_called_once()

    def test_migracion_de_estado_antiguo_sin_envios(self):
        found = discover(self.source, '<main><a href="/a.pdf">A</a></main>')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            state.write_text(json.dumps({"prueba": ["identificador_antiguo"]}))
            with patch("monitor.discover", return_value=found), patch("monitor.send_document") as send:
                self.assertEqual(run([self.source], state, token="test", default_chat="-100"), [])
                send.assert_not_called()
            self.assertEqual(json.loads(state.read_text())["prueba"]["url"], self.source["url"])

    def test_simulacion_sin_estado(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch("monitor.discover", return_value={}):
                self.assertEqual(run([self.source], state, dry_run=True), [])
            self.assertFalse(state.exists())

    def test_heartbeat_por_fuente(self):
        found = discover(self.source, '<main><a href="/a.pdf">A</a></main>')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch("monitor.discover", return_value=found):
                self.assertEqual(run([self.source], state), [])
                record = json.loads(state.read_text())["prueba"]
                self.assertRegex(record["heartbeat_week"], r"^\d{4}-W\d{2}$")
                self.assertEqual(record["last_documents_found"], 1)
                self.assertTrue(record["last_success"].endswith("Z"))
                self.assertEqual(run([self.source], state), [])

    def test_reintentos_web_5_y_15_segundos(self):
        response = Mock()
        response.raise_for_status.return_value = None
        with patch("monitor.requests.get", side_effect=[requests.Timeout(), requests.Timeout(), response]) as req, patch("monitor.time.sleep") as sleep:
            self.assertIs(get("https://example.org/a.pdf"), response)
            self.assertEqual(req.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])

    def test_error_404_no_reintenta(self):
        response = Mock(status_code=404)
        with patch("monitor.requests.get", side_effect=requests.HTTPError(response=response)) as req, patch("monitor.time.sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                get("https://example.org/no.pdf")
            req.assert_called_once()
            sleep.assert_not_called()

    def test_telegram_429_usa_retry_after(self):
        rejected = Mock(status_code=429, headers={})
        rejected.raise_for_status.side_effect = requests.HTTPError(response=rejected)
        rejected.json.return_value = {"parameters": {"retry_after": 8}}
        accepted = Mock()
        accepted.raise_for_status.return_value = None
        accepted.json.return_value = {"ok": True}
        with patch("monitor.requests.post", side_effect=[rejected, accepted]) as post, patch("monitor.time.sleep") as sleep:
            telegram_post("token", {"chat_id": "-100"}, {}, 30)
            self.assertEqual(post.call_count, 2)
            sleep.assert_called_once_with(8)

    def test_error_conserva_ultimo_exito(self):
        found = discover(self.source, '<main><a href="/a.pdf">A</a></main>')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch("monitor.discover", side_effect=[found, ValueError("fallo")]):
                self.assertEqual(run([self.source], state), [])
                previous = json.loads(state.read_text())["prueba"]["last_success"]
                self.assertEqual(run([self.source], state), ["prueba"])
            record = json.loads(state.read_text())["prueba"]
            self.assertEqual(record["last_success"], previous)
            self.assertIn("last_error", record)

    def test_modos_documentos_enlaces_y_ambos(self):
        html = '<main><a href="/a.pdf">PDF</a><a href="/convocatoria">Convocatoria</a></main>'
        self.assertEqual(len(discover(self.source, html)), 1)
        only_links = discover({**self.source, "mode": "links"}, html)
        self.assertEqual([v["kind"] for v in only_links.values()], ["link"])
        both = discover({**self.source, "mode": "both"}, html)
        self.assertEqual({v["kind"] for v in both.values()}, {"link", "document"})

    def test_filtros_enlaces(self):
        html = '<main><a href="/convocatoria">Nueva</a><a href="/contacto">Contacto</a></main>'
        source = {**self.source, "mode": "links", "link_include": ["convocatoria"], "link_exclude": ["contacto"]}
        self.assertEqual(len(discover(source, html)), 1)

    def test_modo_nuevo_inventaria_sin_aviso(self):
        first = discover(self.source, '<main><a href="/a.pdf">PDF</a></main>')
        both = {**self.source, "mode": "both"}
        second = discover(both, '<main><a href="/a.pdf">PDF</a><a href="/pagina">Página</a></main>')
        third = discover(both, '<main><a href="/a.pdf">PDF</a><a href="/pagina">Página</a><a href="/nueva">Nueva</a></main>')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with patch("monitor.discover", side_effect=[first, second, third]), patch("monitor.send_link") as send:
                self.assertEqual(run([self.source], state), [])
                self.assertEqual(run([both], state), [])
                send.assert_not_called()
                self.assertEqual(run([both], state, token="test", default_chat="-100"), [])
                send.assert_called_once()

    def test_notificacion_enlace_usa_send_message(self):
        with patch("monitor.telegram_post") as post:
            send_link(self.source, {"title": "Nueva", "url": "https://example.org/nueva"}, "token", "-100")
            self.assertEqual(post.call_args.kwargs["method"], "sendMessage")
            self.assertIn("https://example.org/nueva", post.call_args.args[1]["text"])

    def test_identificador_duplicado(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "sources.json"
            config.write_text(json.dumps({"sources": [self.source, self.source]}))
            with self.assertRaises(ValueError):
                load_sources(config)


if __name__ == "__main__":
    unittest.main()
