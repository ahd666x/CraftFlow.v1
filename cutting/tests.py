import xml.etree.ElementTree as ET
from django.test import SimpleTestCase
from .optimizer import optimize, make_autocut

class CuttingOptimizerTests(SimpleTestCase):
    def test_uses_offcut_first_and_places_rotated_part(self):
        stock=[{'width':900,'height':700,'is_offcut':False,'label':'full'}, {'width':650,'height':500,'is_offcut':True,'label':'offcut'}]
        parts=[{'width':480,'height':620,'orig_width':480,'orig_height':620,'turn':True,'uid':0,'label':'panel'}]
        layouts=optimize(parts,stock,kerf=4)
        self.assertEqual(len(layouts),1)
        self.assertTrue(layouts[0]['sheet']['is_offcut'])
        self.assertTrue(layouts[0]['parts'][0]['rotated'])

    def test_impossible_piece_is_reported(self):
        with self.assertRaisesRegex(ValueError,'موجودی کافی نیست'):
            optimize([{'width':1000,'height':800,'turn':False,'label':'too big'}],[{'width':500,'height':500,'is_offcut':False}],4)

    def test_generated_cut_is_xml_with_objectives_and_layouts(self):
        row={'type':'Oak-16','thickness':'16.0','shapes':[{'orig_width':200,'orig_height':300,'turn':True,'uid':0,'f2':'side'}], 'sheets':[{'sheet':{'width':900,'height':700,'label':'sheet A'},'parts':[{'width':200,'height':300,'x':0,'y':0,'uid':0,'rotated':False}]}]}
        payload=make_autocut(1,[row])
        root=ET.fromstring(payload)
        ns={'a':'http://www.King-stone.com'}
        self.assertEqual(root.tag,'{http://www.King-stone.com}AutoCUT')
        self.assertEqual(root.find('.//a:Objective',ns).attrib['Count'],'1')
        self.assertEqual(root.find('.//a:Layout',ns).attrib['X'],'900')
