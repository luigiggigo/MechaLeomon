import json
import os
import queue
import tempfile
import threading
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock, patch

from auto_digiworld_batch2 import controlled_wait
from control import ControlFiles
from pvp_damage import compare, hits_to_kill, report, rounded
from pvp_ocr import CaptureSession, Page, SwipeGeometry, Token, parse_page, self_test
from pvp_stats import LABELS, REQUIRED, Field, Sheet, parse_number

FIXTURE = Path(__file__).parent/'fixtures'/'pvp_reference.json'


def blank():
    values = dict.fromkeys(REQUIRED,D(0))
    values.update({'Combined ATK':D(100),'Combined DEF':D(100),'Combined HP':D(1000)})
    return values


def tokens(labels, scale=1):
    rows = [Token('Detailed Stats',.99,300,20,200,20), Token('You',.99,400,60,50,20), Token('Target',.99,650,60,60,20)]
    for i,label in enumerate(labels):
        y=110+i*35
        rows.extend([Token(label,.99,140,y,180,20),Token('102.41%',.99,400,y,100,20),Token('926.504%',.99,650,y,100,20)])
    return [Token(t.text,t.confidence,t.x*scale,t.y*scale,t.width*scale,t.height*scale) for t in rows]


class ParsingTests(unittest.TestCase):
    def test_formats(self):
        for text,value in [('9,002,503',9002503),('559,357,077',559357077),('1.78',D('1.78'))]:
            self.assertEqual(parse_number(text),value)
        for text,value in [('102.41%',D('1.0241')),('926.504%',D('9.26504')),('85,876.00%',D('858.76')),('0.00%',0)]:
            self.assertEqual(parse_number(text,True),value)
        for text in ('','O','1,23','NaN','-1','1e6','1.2.3','10%%'):
            with self.assertRaises(ValueError): parse_number(text,True)

    def test_missing_and_correction(self):
        sheet=Sheet()
        with self.assertRaises(ValueError): sheet.values()
        for label in REQUIRED: sheet.correct(label,'100' if label.startswith('Combined') else '0')
        first=compare(sheet.values(),sheet.values())
        sheet.fields['Normal Attack Damage']=Field('100',.3,'suspicious')
        with self.assertRaises(ValueError): sheet.values()
        sheet.correct('Normal Attack Damage','100%')
        self.assertEqual(compare(sheet.values(),sheet.values())['attacks']['Normal Attack']['noncrit'],first['attacks']['Normal Attack']['noncrit']*2)

    def test_columns_scaling_and_legacy_context(self):
        for scale in (.6,1,1.8):
            page=parse_page(tokens(['Damage DOWN','Support Damage','Support Damage DOWN','Skill Damage'],scale))
            self.assertIn('Normal Attack Damage',page.rows)
            self.assertNotIn('Support Damage',page.rows)
            self.assertEqual(page.rows['Normal Attack Damage'][1].text,'926.504%')
        page=parse_page(tokens(['Counterattack Damage DOWN','Support Damage','Support Damage DOWN','Normal Attack Damage UP']))
        self.assertIn('Support Damage',page.rows)
        page=parse_page(tokens(['Support Damage UP','Normal Attack Damage RESIST','Skill Damage UP']))
        self.assertIn('Normal Attack Damage UP',page.rows)

    def test_swipe_geometry_scales_and_keeps_two_rows_overlapping(self):
        labels=LABELS[8:20]
        for scale in (.6,1,1.8):
            page=parse_page(tokens(labels,scale))
            self.assertEqual(page.visible_labels,labels)
            self.assertEqual(page.swipe,SwipeGeometry(
                round(650*scale),round((110+10*35)*scale),round((110+2*35)*scale)))
            self.assertEqual(page.rewind,SwipeGeometry(
                round(650*scale),round(110*scale),round((110+11*35)*scale)))
        page=parse_page(tokens(['Critical Damage RESIST','Support Damage UP','Support Damage RESIST','HP Recovery']))
        self.assertIn('Support Damage UP',page.rows)

    def test_ambiguous_support_not_guessed(self):
        page=parse_page(tokens(['Support Damage','Support Damage UP','HP Recovery','HP Recovery Rate']))
        self.assertNotIn('Support Damage',page.rows)
        self.assertNotIn('Normal Attack Damage',page.rows)

    def test_not_stats_and_malformed_ocr(self):
        with self.assertRaises(ValueError): parse_page(tokens(['Skill Damage','Damage DOWN'])[1:])
        rows=tokens(['Skill Damage','Damage DOWN'])
        rows[4]=Token('O.OO%',.99,400,110,100,20)
        page=parse_page(rows)
        self.assertEqual(page.rows['Skill Damage'][0].status,'suspicious')

    def test_split_detailed_stats_title_is_accepted(self):
        rows=tokens(['Skill Damage','Damage DOWN'])
        rows[0:1]=[
            Token('Detailed',.99,245,20,95,20),
            Token('Stats',.99,350,20,70,20),
        ]
        self.assertIn('Skill Damage',parse_page(rows).rows)

    def test_real_layout_combined_header_shares_column_header_row(self):
        rows=[
            Token('Detailed Stats',.99,315,364,258,37),
            Token('Combined Stats',.99,275,447,204,30),
            Token('You',.99,484,447,59,36),
            Token('Target',.99,678,446,92,36),
        ]
        for i,label in enumerate(('ATK','DEF','HP','Attack Speed')):
            y=498+i*48
            rows.extend((Token(label,.99,220,y,130,28),
                         Token('100',.99,484,y,80,28),
                         Token('200',.99,678,y,80,28)))
        rows.append(Token('Basic Stats',.99,244,725,142,30))
        for i,label in enumerate(('ATK','DEF','HP','Attack Speed','ATK Bonus',
                                  'DEF Bonus','HP Bonus')):
            y=780+i*50
            rows.extend((Token(label,.99,220,y,130,28),
                         Token('100',.99,484,y,80,28),
                         Token('200',.99,678,y,80,28)))
        page=parse_page(rows)
        self.assertTrue({'Combined ATK','Combined DEF','Combined HP'}.issubset(page.rows))
        self.assertTrue({'ATK','DEF','HP'}.issubset(page.rows))
        self.assertEqual(page.swipe,SwipeGeometry(678,1030,880))
        self.assertEqual(page.rewind,SwipeGeometry(678,780,1080))

    def test_scroll_geometry_survives_lower_section_header_leaving_view(self):
        rows=[
            Token('Detailed Stats',.99,315,364,258,37),
            Token('Combined Stats',.99,275,447,204,30),
            Token('You',.99,484,447,59,36),
            Token('Target',.99,678,446,92,36),
        ]
        for i,label in enumerate(('ATK','DEF','HP','Attack Speed')):
            y=498+i*48
            rows.extend((Token(label,.99,220,y,130,28),
                         Token('100',.99,484,y,80,28),
                         Token('200',.99,678,y,80,28)))
        for i,label in enumerate(LABELS[12:19]):
            y=780+i*50
            rows.extend((Token(label,.99,220,y,180,28),
                         Token('100%',.99,484,y,80,28),
                         Token('200%',.99,678,y,80,28)))
        page=parse_page(rows)
        self.assertEqual(page.swipe,SwipeGeometry(678,1030,880))
        self.assertEqual(page.rewind,SwipeGeometry(678,780,1080))

    def test_merge_conflict_manual_preserved_and_end(self):
        session=CaptureSession()
        page=parse_page(tokens(['HP Recovery','HP Recovery Rate','HP Recovery Amount']))
        self.assertEqual(session.merge(page),6)
        self.assertTrue(session.final)
        self.assertEqual(session.merge(page),0)
        pair=(Field('4%',.99,'recognized'),)*2
        session.merge(Page({'HP Recovery':pair},False))
        self.assertEqual(session.sheets[0].fields['HP Recovery'].status,'conflict')
        session.sheets[0].correct('HP Recovery','5%')
        session.merge(page)
        self.assertEqual(session.sheets[0].fields['HP Recovery'].text,'5%')

    def test_merge_equal_value_in_different_format_is_recognized(self):
        session=CaptureSession()
        session.merge(parse_page(tokens(['HP Recovery','HP Recovery Rate','HP Recovery Amount'])))
        # Overlap re-capture reads the same number without the trailing zero.
        session.merge(Page({'HP Recovery':(Field('102.410%',.99,'recognized'),)*2},False))
        field=session.sheets[0].fields['HP Recovery']
        self.assertEqual((field.status,field.text),('recognized','102.41%'))
        # Basic rows: a dropped thousands separator is the same number too.
        session.merge(Page({'ATK':(Field('10,476',.99,'recognized'),)*2},False))
        session.merge(Page({'ATK':(Field('10476',.99,'recognized'),)*2},False))
        field=session.sheets[0].fields['ATK']
        self.assertEqual((field.status,field.text),('recognized','10,476'))
        # Two valid readings that really differ stay conflicted for review.
        session.merge(Page({'HP Recovery':(Field('4%',.99,'recognized'),)*2},False))
        self.assertEqual(session.sheets[0].fields['HP Recovery'].status,'conflict')

    def test_merge_glitched_reread_never_invalidates_recognized_value(self):
        session=CaptureSession()
        session.merge(parse_page(tokens(['HP Recovery','HP Recovery Rate','HP Recovery Amount'])))
        # Second capture: the You column match fails entirely and the Target
        # column gains a glued arrow glyph; neither may downgrade the values
        # the previous page already recognized.
        glitch=Page({'HP Recovery':(Field('',.4,'suspicious'),Field('926.504%↑',.4,'suspicious'))},False)
        session.merge(glitch)
        you,target=session.sheets[0].fields['HP Recovery'],session.sheets[1].fields['HP Recovery']
        self.assertEqual((you.status,you.text),('recognized','102.41%'))
        self.assertEqual((target.status,target.text),('recognized','926.504%'))

    def test_agreeing_reread_resolves_conflict(self):
        session=CaptureSession()
        session.merge(parse_page(tokens(['HP Recovery','HP Recovery Rate','HP Recovery Amount'])))
        session.merge(Page({'HP Recovery':(Field('4%',.99,'recognized'),)*2},False))
        self.assertEqual(session.sheets[0].fields['HP Recovery'].status,'conflict')
        session.merge(Page({'HP Recovery':(Field('102.41%',.99,'recognized'),)*2},False))
        field=session.sheets[0].fields['HP Recovery']
        self.assertEqual((field.status,field.text),('recognized','102.41%'))

    def test_duplicate_row_with_equal_reading_is_not_a_conflict(self):
        page=parse_page(tokens(['Skill Damage','Skill Damage','HP Recovery Rate']))
        self.assertEqual(page.rows['Skill Damage'][0].status,'recognized')

    def test_duplicate_row_with_different_values_still_conflicts(self):
        rows=tokens(['Skill Damage','Skill Damage','HP Recovery Rate'])
        rows[7]=Token('4%',.99,400,145,100,20)
        page=parse_page(rows)
        self.assertEqual(page.rows['Skill Damage'][0].status,'conflict')

    def test_wrapped_down_label_is_joined_with_block_values(self):
        rows=[Token('Detailed Stats',.99,300,20,200,20),Token('You',.99,400,60,50,20),Token('Target',.99,650,60,60,20)]
        rows += [Token('Critical Rate',.99,140,110,180,20),Token('10.00%',.99,400,110,100,20),Token('20.00%',.99,650,110,100,20)]
        # 'Skill Critical Damage DOWN' renderizzato su due righe, valori
        # centrati verticalmente sul blocco come nello screenshot reale.
        rows += [Token('Skill Critical Damage',.99,140,150,180,20),Token('DOWN',.99,140,175,60,20)]
        rows += [Token('315.90%',.99,400,163,100,20),Token('323.630%',.99,650,163,100,20)]
        page=parse_page(rows)
        self.assertEqual(page.rows['Critical Rate'][0].text,'10.00%')
        self.assertNotIn('Skill Critical Damage',page.rows)
        self.assertEqual(page.rows['Skill Critical Damage DOWN'][0].text,'315.90%')
        self.assertEqual(page.rows['Skill Critical Damage DOWN'][1].text,'323.630%')

    def test_wrapped_label_after_identical_single_line_row(self):
        rows=[Token('Detailed Stats',.99,300,20,200,20),Token('You',.99,400,60,50,20),Token('Target',.99,650,60,60,20)]
        rows += [Token('Skill Critical Damage',.99,140,110,180,20),Token('100.00%',.99,400,110,100,20),Token('200.00%',.99,650,110,100,20)]
        rows += [Token('Skill Critical Damage',.99,140,150,180,20),Token('DOWN',.99,140,175,60,20)]
        rows += [Token('315.90%',.99,400,163,100,20),Token('323.630%',.99,650,163,100,20)]
        page=parse_page(rows)
        self.assertEqual(page.rows['Skill Critical Damage'][0].text,'100.00%')
        self.assertEqual(page.rows['Skill Critical Damage DOWN'][0].text,'315.90%')
        self.assertEqual(page.rows['Skill Critical Damage DOWN'][1].text,'323.630%')

    def test_real_ocr_supporter_damage_resist_alias_is_joined(self):
        rows=tokens(['Critical Damage RESIST','Support Damage','HP Recovery'])
        rows[6]=Token('Supporter Damage',.999,140,137,180,20)
        rows.insert(7,Token('RESIST',.94,140,157,70,20))
        rows[8]=Token('2.28%',.999,400,147,100,20)
        rows[9]=Token('3.200%',.999,650,147,100,20)
        page=parse_page(rows)
        self.assertEqual(page.rows['Support Damage RESIST'][0].text,'2.28%')
        self.assertEqual(page.rows['Support Damage RESIST'][1].text,'3.200%')

    def test_reference_ocr_tokens(self):
        data=json.loads(FIXTURE.read_text())
        for label in data['You']:
            if label.startswith('Combined'): continue
            rows=tokens([label,'HP Recovery Rate'])
            rows[4]=Token(data['You'][label]+'%',.99,400,110,100,20)
            rows[5]=Token(data['Target'][label]+'%',.99,650,110,100,20)
            # Support needs adjacent canonical evidence, tested separately above.
            if label.startswith('Support'): continue
            page=parse_page(rows)
            self.assertEqual(page.rows[label][0].text,data['You'][label]+'%')


class DamageTests(unittest.TestCase):
    def test_real_regression_both_directions(self):
        data=json.loads(FIXTURE.read_text())
        sheets=[]
        for name in ('You','Target'):
            sheet=blank()  # unprovided chance/recovery fields are explicit test-only zeroes
            for key,text in data[name].items(): sheet[key]=parse_number(text,not key.startswith('Combined'))
            sheets.append(sheet)
        expected=[[(35942501,670957728),(13279394,49834910),(18382246,343151146),(10644802,198712170),(40165420,749789188)],[(32409175,251817610),(3829263,5743895),(4313282,33513984),(3829263,29753182),(39307547,305417605)]]
        for i,(a,d) in enumerate((sheets,sheets[::-1])):
            result=compare(a,d)
            for packet,pair in zip(result['attacks'].values(),expected[i]):
                self.assertEqual((packet['noncrit'],packet['crit']),pair)
            self.assertEqual(result['hit_chance'],D('0.6267' if i==0 else '0.1888'))
            self.assertEqual(result['counter_chance'],0)
            self.assertEqual(result['attacks']['Normal Attack']['crit_hits'],1 if i==0 else 3)
        self.assertIn('Combo Chance DOWN',report(*sheets))
        summary=report(*sheets)
        self.assertIn('Static matchup edge: YOU',summary)
        self.assertIn('Normal Attack crit_one_shot',summary)
        self.assertIn('YES',summary)

    def test_layers_and_crit_every_path(self):
        a,d=blank(),blank()
        a['Critical Damage']=D(1); a['Critical Damage UP']=D(2)
        a['Skill Critical Damage']=D(3)
        for kind in ('Normal Attack','Skill','Combo','Counterattack','Support'):
            a[kind+' Damage']=D(2); a[kind+' Damage UP']=D(1)
        result=compare(a,d,2)
        for kind,packet in result['attacks'].items():
            damage=300*(2 if kind=='Skill' else 3 if kind=='Support' else 1)
            self.assertEqual(packet['noncrit'],damage)
            self.assertEqual(packet['crit'],int(damage*(4.5 if kind=='Skill' else 7.5)))
        d['Damage DOWN']=D(9)
        for kind in ('Normal Attack','Skill','Combo','Counterattack','Support'): d[kind+' Damage RESIST']=D(9)
        self.assertEqual(compare(a,d)['attacks']['Normal Attack']['noncrit'],50)
        self.assertEqual(compare(a,d)['attacks']['Support']['noncrit'],150)

    def test_boss_ignored_and_skill_crit_no_amplify(self):
        a,d=blank(),blank()
        before=compare(a,d)
        a['Boss Damage']=D(999); d['Boss Damage DOWN']=D(999)
        self.assertEqual(compare(a,d),before)
        a['Critical Damage UP']=D(999)
        self.assertEqual(compare(a,d)['attacks']['Skill']['crit'],75)

    def test_probabilities_counter_and_recovery(self):
        a,d=blank(),blank()
        a.update({'Combo Rate':D('.5'),'Counterattack Rate':D('.6'),'HP Recovery':D('.1'),'HP Recovery Rate':D('.2'),'HP Recovery Amount':D('.3')})
        d.update({'Evasion Rate':D(2),'Combo Chance DOWN':D(9),'Counterattack Chance DOWN':D('.2')})
        r=compare(a,d)
        self.assertEqual(r['hit_chance'],0)
        self.assertEqual(r['attacks']['Counterattack']['hit_chance'],1)
        self.assertEqual(r['combo_rate'],D('.5'))
        self.assertEqual(r['counter_chance'],D('.4'))
        self.assertEqual((r['timed_heal'],r['heal_chance'],r['heal_amount']),(100,D('.2'),300))
        a['Evasion Rate DOWN']=D(3)
        self.assertEqual(compare(a,d)['hit_chance'],1)

    def test_rounding_validation_and_helpers(self):
        self.assertEqual([rounded(D(v)) for v in ('0','.01','1.99','100.99')],[0,1,1,100])
        self.assertEqual(hits_to_kill(D(100),33),4)
        self.assertIsNone(hits_to_kill(D(100),0))
        for coefficient in (0,-1,'NaN','Infinity'):
            with self.assertRaises(ValueError): compare(blank(),blank(),coefficient)
        a=blank(); del a['Skill Damage']
        with self.assertRaises(ValueError): compare(a,blank())


class PauseTests(unittest.TestCase):
    def test_pause_ack_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            controls=ControlFiles(Path(tmp)); controls.pause()
            thread=threading.Thread(target=controlled_wait,args=(controls.pause_file,controls.stop_file,0))
            thread.start()
            try:
                deadline=time.monotonic()+2
                while not controls.pause_ack_file.exists() and time.monotonic()<deadline: time.sleep(.01)
                self.assertTrue(controls.pause_ack_file.exists())
                self.assertTrue(thread.is_alive())
            finally:
                controls.stop(); thread.join(2)
            self.assertFalse(controls.pause_ack_file.exists())

    def test_close_preserves_manual_pause_and_stop(self):
        from pvp_gui import AnalyzerWindow
        for manually_paused,stopped in ((False,False),(True,False),(False,True)):
            window=AnalyzerWindow.__new__(AnalyzerWindow)
            window.busy=False; window.had_worker=True; window.was_paused=manually_paused
            window.window=Mock(); owner=Mock(); owner.closing=False
            owner.process.poll.return_value=None
            owner.controls.stop_file.exists.return_value=stopped
            window.owner=owner; window.original_process=owner.process
            window.close()
            self.assertEqual(owner.controls.resume.called,not manually_paused and not stopped)
            if not manually_paused and not stopped:
                owner.set_state.assert_called_with(
                    'running', 'Automazione ripresa dopo l’analisi PvP.')


class OfflineOCRTests(unittest.TestCase):
    def test_telemetry_is_disabled_before_onnxruntime_import(self):
        self.assertEqual(os.environ.get('ORT_DISABLE_TELEMETRY'),'1')

    def test_bundled_models_execute_without_network(self):
        with patch('socket.socket',side_effect=AssertionError('OCR must be offline')):
            self.assertTrue(self_test())

class CaptureSafetyTests(unittest.TestCase):
    def window(self):
        from pvp_gui import AnalyzerWindow
        window=AnalyzerWindow.__new__(AnalyzerWindow)
        window.adb='adb'; window.serial='explicit-device'
        window.requested_serial='explicit-device'; window.results=queue.Queue()
        window.backend=Mock(); window.session=CaptureSession()
        window.highest_scanned_index=-1; window.cancel_event=threading.Event()
        window.closed=False; window.scan_log=None
        window.owner=Mock(); window.owner.closing=False
        window.owner.controls.stop_file.stat.side_effect=FileNotFoundError
        window.scan_stop_marker=None
        window._log=Mock()
        return window

    def test_stale_stop_file_does_not_cancel_new_scan(self):
        window=self.window()
        marker=Mock(st_mtime_ns=123)
        window.owner.controls.stop_file.stat.side_effect=None
        window.owner.controls.stop_file.stat.return_value=marker
        window.scan_stop_marker=123
        self.assertTrue(window._continue_after(0))

    def test_stop_created_after_scan_start_cancels_scan(self):
        window=self.window()
        window.scan_stop_marker=None
        window.owner.controls.stop_file.stat.side_effect=None
        window.owner.controls.stop_file.stat.return_value=Mock(st_mtime_ns=456)
        self.assertFalse(window._continue_after(0))

    @staticmethod
    def page(labels,final=False):
        pair=(Field('100',.99,'recognized'),)*2
        forward = len(labels) >= 6
        rewind = len(labels) >= 2
        return Page(dict.fromkeys(labels,pair),final,tuple(labels),
                    SwipeGeometry(650,320,180) if forward else None,
                    SwipeGeometry(650,180,320) if rewind else None)

    def test_scan_waits_for_worker_ack(self):
        from pvp_gui import AnalyzerWindow
        window=AnalyzerWindow.__new__(AnalyzerWindow)
        window.busy=False; window.ready=False
        with patch('pvp_gui.threading.Thread') as thread:
            window.toggle_scan()
        thread.assert_not_called()

    def test_automatic_scan_scrolls_until_final_page(self):
        window=self.window()
        first=self.page(LABELS[:8])
        final=self.page(LABELS[8:],True)
        window._capture_page=Mock(side_effect=[first,final])
        sent=Mock(outcome='sent',serial='explicit-device')
        with patch('digiworld_bot.dispatch_swipe_with_recovery',return_value=sent) as swipe:
            window.scan_automatic()
        self.assertEqual([event[0] for event in list(window.results.queue)],
                         ['page','page','complete'])
        swipe.assert_called_once()
        self.assertEqual(swipe.call_args.args[:7],
                         ('adb','explicit-device',650,320,650,180,600))

    def test_scan_rewinds_to_initial_page_before_collecting(self):
        window=self.window()
        middle=self.page(LABELS[:4]+LABELS[10:18])
        initial=self.page(LABELS[:8])
        final=self.page(LABELS[8:],True)
        window._capture_page=Mock(side_effect=[middle,initial,final])
        sent=Mock(outcome='sent',serial='explicit-device')
        with patch('digiworld_bot.dispatch_swipe_with_recovery',return_value=sent) as swipe:
            window.scan_automatic()
        self.assertEqual([event[0] for event in list(window.results.queue)],
                         ['page','page','complete'])
        self.assertEqual(swipe.call_count,2)
        self.assertEqual(swipe.call_args_list[0].kwargs['action'],
                         'pvp_stats_rewind')
        self.assertEqual(swipe.call_args_list[1].kwargs['action'],
                         'pvp_stats_scroll')

    def test_unreliable_rewind_geometry_sends_no_input(self):
        window=self.window()
        page=self.page(LABELS[10:11])
        self.assertIsNone(page.rewind)
        window._capture_page=Mock(return_value=page)
        with patch('digiworld_bot.dispatch_swipe_with_recovery') as swipe:
            window.scan_automatic()
        self.assertEqual(list(window.results.queue)[-1][0],'error')
        swipe.assert_not_called()

    def test_repeated_page_stops_without_second_swipe(self):
        window=self.window()
        page=self.page(LABELS[:8])
        window._capture_page=Mock(side_effect=[page,page])
        sent=Mock(outcome='sent',serial='explicit-device')
        with patch('digiworld_bot.dispatch_swipe_with_recovery',return_value=sent) as swipe:
            window.scan_automatic()
        self.assertEqual([event[0] for event in list(window.results.queue)],
                         ['page','page','stopped'])
        swipe.assert_called_once()

    def test_post_swipe_ocr_is_retried_without_another_input(self):
        import digiworld_bot as bot
        window=self.window()
        page=self.page(LABELS[8:16])
        window.backend.read.return_value=[]
        with (patch.object(bot,'screenshot',return_value=Mock()) as screenshot,
              patch('pvp_gui.parse_page',side_effect=[ValueError('moving'),
                                                     ValueError('moving'),page]),
              patch.object(window,'_continue_after',return_value=True)):
            result=window._capture_page(bot,window.backend,True)
        self.assertIs(result,page)
        self.assertEqual(screenshot.call_count,3)

    def test_cancel_before_capture_preserves_session(self):
        window=self.window(); window.cancel_event.set()
        with patch('digiworld_bot.screenshot') as screenshot:
            window.scan_automatic()
        screenshot.assert_not_called()
        self.assertEqual(window.results.get_nowait()[0],'cancelled')
        self.assertEqual(window.session.pages,0)

    def test_capture_failure_is_reported_not_retried(self):
        window=self.window()
        with patch('digiworld_bot.screenshot',side_effect=OSError('transport failure')) as capture:
            window.scan_automatic()
        capture.assert_called_once()
        event=window.results.get_nowait()
        self.assertEqual((event[0],event[1]),('error','transport failure'))

    def test_invalid_manual_edit_replaces_prior_valid_value(self):
        from pvp_gui import AnalyzerWindow
        window=AnalyzerWindow.__new__(AnalyzerWindow)
        window.session=CaptureSession()
        window.session.sheets[0].correct('Skill Damage','100')
        variable=Mock(); variable.get.return_value='O'
        window.entries={(0,'Skill Damage'):(variable,Mock(),Mock())}
        window.edit(0,'Skill Damage')
        field=window.session.sheets[0].fields['Skill Damage']
        self.assertEqual(field.text,'O')
        self.assertEqual(field.status,'suspicious')

    def test_synthetic_image_end_to_end(self):
        from PIL import Image, ImageDraw, ImageFont
        from pvp_ocr import LocalOCR
        image=Image.new('RGB',(1000,450),'white')
        draw=ImageDraw.Draw(image); font=ImageFont.load_default(size=24)
        draw.text((330,20),'Detailed Stats',font=font,fill='black')
        draw.text((560,75),'You',font=font,fill='black')
        draw.text((820,75),'Target',font=font,fill='black')
        for i,label in enumerate(('HP Recovery','HP Recovery Rate','HP Recovery Amount')):
            y=145+i*80
            draw.text((25,y),label,font=font,fill='black')
            draw.text((530,y),'102.41%',font=font,fill='black')
            draw.text((800,y),'926.50%',font=font,fill='black')
        page=parse_page(LocalOCR().read(image))
        self.assertTrue(page.final)
        self.assertEqual(page.rows['HP Recovery'][0].text,'102.41%')
        self.assertEqual(page.rows['HP Recovery'][1].text,'926.50%')

class TkReviewTests(unittest.TestCase):
    def test_window_manual_review_and_recalculation_without_adb(self):
        import tkinter as tk

        import app
        try:
            root=tk.Tk()
        except tk.TclError:
            self.skipTest('Tk display unavailable; exercised by Windows build')
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(app,'data_root',return_value=Path(tmp)), patch.object(root,'after'), patch('digiworld_bot.screenshot') as capture:
                owner=app.ExplorerWindow(root)
                owner.open_pvp()
                window=owner.pvp_window
                for side in range(2):
                    for label in REQUIRED:
                        window.entries[side,label][0].set('1000' if label=='Combined HP' else '100' if label.startswith('Combined') else '0')
                window.calculate()
                first=window.output.get('1.0','end')
                self.assertIn('You -> Target',first)
                window.coefficient.set('4')
                window.calculate()
                self.assertNotEqual(window.output.get('1.0','end'),first)
                self.assertIn('Skill coefficient: 4',window.output.get('1.0','end'))
                capture.assert_not_called()
                window.close()
                self.assertIsNone(owner.pvp_window)
        finally:
            root.destroy()
