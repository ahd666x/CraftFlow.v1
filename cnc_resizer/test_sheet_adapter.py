"""Tests for the dialect-locked sheet postprocessor (minimal_xy_router).

The fixture ``sample.cnc`` is a real program from the workshop controller
(the 65-file archive dialect): two-stage tool change with ``M141``, I/J arcs
only, ``G53`` park lines and ``N`` numbers stepping by 10.
"""
import os

from django.test import TestCase

from .tests import SAMPLE
from .sheet_adapter import (
    DIALECT_ADAPTER_KEY,
    SheetAdapterError,
    analyze_piece,
    build_sheet_program,
)
from .sheet_nesting import _with_explicit_mm
from .gcode_sheet import source_bounds, split_program


def _sample_mm():
    text, _injected = _with_explicit_mm(open(SAMPLE, encoding='utf-8-sig').read())
    return text


def _patched_sample(**changes):
    text = _sample_mm()
    if changes.get('tool'):
        tool = changes['tool']
        text = text.replace('T2M6', f'T{tool}M6').replace('M6T2', f'M6T{tool}')
        text = text.replace('G43 H2', f'G43 H{tool}')
    if changes.get('no_m141'):
        text = text.replace('N40 M141\n', '')
    if changes.get('extra_body'):
        lines = text.splitlines(keepends=True)
        anchor = next(i for i, l in enumerate(lines) if l.strip().startswith('N90G1X'))
        lines.insert(anchor, changes['extra_body'])
        text = ''.join(lines)
    return text


def _lines_with(text, needle):
    return [line for line in text.split('\r\n') if needle in line]


class AnalyzePieceTests(TestCase):
    def test_verified_dialect_sample_is_accepted(self):
        structure = analyze_piece(_sample_mm())
        self.assertEqual(structure['tool'], 2)
        self.assertEqual(structure['h'], '2')
        self.assertTrue(structure['m141'])
        self.assertEqual(structure['post'], ['G53Z0', 'G53X0Y3950'])
        # the trailing G0X0Y0 program park is stripped from the body
        self.assertEqual(structure['body'][-1], 'G0Z5.000M3 S16000')

    def test_r_format_arcs_are_rejected(self):
        text = _sample_mm().replace('N140G3X113.440Y65.477I+0.838J+0.629',
                                    'N140G3X113.440Y65.477R1.05')
        with self.assertRaisesRegex(SheetAdapterError, 'R مجاز|I/J'):
            analyze_piece(text)

    def test_relative_and_inch_modes_are_rejected(self):
        with self.assertRaisesRegex(SheetAdapterError, 'G91'):
            analyze_piece(_sample_mm().replace('N10G90G54', 'N10G91G54'))
        with self.assertRaisesRegex(SheetAdapterError, 'G20'):
            analyze_piece(_sample_mm().replace('N10G90G54', 'N10G20G90G54'))

    def test_macro_constructs_are_rejected(self):
        with self.assertRaisesRegex(SheetAdapterError, 'دیالکت'):
            analyze_piece(_sample_mm().replace('N80G1Z-12.000F15000.0', 'N80G1Z-12.000F15000.0\nN85IF[#1GT5]GOTO90'))

    def test_first_stage_tool_change_inside_body_is_rejected(self):
        text = _patched_sample(extra_body='N85T7M6\n')
        with self.assertRaisesRegex(SheetAdapterError, 'M6T#'):
            analyze_piece(text)

    def test_body_must_end_with_rapid_retract(self):
        text = _sample_mm().replace('N2260G0Z5.000M3 S16000\n', '')
        with self.assertRaisesRegex(SheetAdapterError, 'ارتفاع\u200cگیر'):
            analyze_piece(text)

    def test_piece_without_explicit_millimetres_is_rejected(self):
        text = open(SAMPLE, encoding='utf-8-sig').read()
        with self.assertRaisesRegex(SheetAdapterError, 'G21'):
            analyze_piece(text)

    def test_missing_g53_park_is_rejected(self):
        text = _sample_mm().replace('N2280G53Z0\n', '').replace('N2290G53X0Y3950\n', '')
        with self.assertRaisesRegex(SheetAdapterError, 'G53'):
            analyze_piece(text)

    def test_mid_program_second_stage_switch_is_tracked(self):
        text = _patched_sample(extra_body='M6T7\nG43 H7\n')
        structure = analyze_piece(text)
        kinds = [kind for entries in structure['switches'].values() for kind, _v in entries]
        self.assertEqual(kinds, ['tool', 'h'])


class BuildSheetProgramTests(TestCase):
    def _program(self, placements):
        return build_sheet_program(placements, DIALECT_ADAPTER_KEY)

    def test_unknown_adapter_key_is_rejected(self):
        with self.assertRaisesRegex(SheetAdapterError, 'minimal_xy_router'):
            build_sheet_program([{'text': _sample_mm(), 'place_x': 0, 'place_y': 0}], 'other')

    def test_single_header_m141_and_park_for_repeated_pieces(self):
        piece = _sample_mm()
        program = self._program([
            {'name': 'p1', 'text': piece, 'place_x': 10, 'place_y': 20},
            {'name': 'p2', 'text': piece, 'place_x': 500, 'place_y': 20},
            {'name': 'p3', 'text': piece, 'place_x': 990, 'place_y': 20},
        ])
        lines = program.split('\r\n')
        self.assertEqual(lines[0], '%')
        self.assertEqual(lines[-1], '')
        self.assertEqual(lines[-2], '%')
        self.assertEqual(lines[1:7], [
            'N10G90G54', 'N20G53Z0', 'N30T2M6', 'N40M141', 'N50M6T2', 'N60G43H2',
        ])
        self.assertEqual(program.count('M141'), 1)
        self.assertEqual(program.count('G53X0Y3950'), 1)
        self.assertNotIn('G0X0Y0', program)
        self.assertEqual(lines[-5:-2], ['N6670G53Z0', 'N6680G53X0Y3950', 'N6690M30'])
        # N numbering steps by 10 across the whole program
        import re
        numbered = [line for line in lines if line.startswith('N')]
        numbers = [int(re.match(r'N(\d+)', line).group(1)) for line in numbered]
        self.assertEqual(numbers, list(range(10, 10 * (len(numbers) + 1), 10)))
        # exactly three bodies: one approach rapid per placement
        approaches = [line for line in lines if 'G0X' in line and 'Z5.000' in line]
        self.assertEqual(len(approaches), 3)

    def test_placed_coordinates_follow_layout(self):
        piece = _sample_mm()
        _pre, body, _post = split_program(piece)
        min_x, min_y, _max_x, _max_y = source_bounds(body)
        program = self._program([{'name': 'p1', 'text': piece, 'place_x': 100.5, 'place_y': 50.25}])
        first_move = _lines_with(program, 'G0X')[0]
        expected_x = 100.5 + (113.194 - min_x)
        expected_y = 50.25 + (67.004 - min_y)
        self.assertIn(f'X{expected_x:.3f}'.rstrip('0').rstrip('.'), first_move)
        self.assertIn(f'Y{expected_y:.3f}'.rstrip('0').rstrip('.'), first_move)
        self.assertNotIn('X0Y0', program.split('N60G43H2', 1)[1].rsplit('G53', 1)[0])

    def test_rotated_piece_maps_xy_and_ij(self):
        piece = _sample_mm()
        _pre, body, _post = split_program(piece)
        min_x, min_y, max_x, _max_y = source_bounds(body)
        program = self._program([{'name': 'p1', 'text': piece, 'place_x': 30, 'place_y': 40, 'rotated': True}])
        arc_line = _lines_with(program, 'G3')[0]
        # source arc: N140G3X113.440Y65.477I+0.838J+0.629 -> rotated (x,y)->(y-min_y+px, max_x-x+py), (i,j)->(j,-i)
        expected_x = (65.477 - min_y) + 30
        expected_y = (max_x - 113.440) + 40
        self.assertIn(f'X{expected_x:.3f}'.rstrip('0').rstrip('.'), arc_line)
        self.assertIn(f'Y{expected_y:.3f}'.rstrip('0').rstrip('.'), arc_line)
        self.assertIn('I0.629', arc_line)
        self.assertIn('J-0.838', arc_line)

    def test_two_tool_sheet_switches_without_repeating_m141(self):
        piece_a = _sample_mm()
        piece_b = _patched_sample(tool=7)
        program = self._program([
            {'name': 'a', 'text': piece_a, 'place_x': 10, 'place_y': 10},
            {'name': 'b', 'text': piece_b, 'place_x': 600, 'place_y': 10},
        ])
        self.assertEqual(program.count('M141'), 1)
        lines = program.split('\r\n')
        switch_index = next(i for i, line in enumerate(lines) if 'M6T7' in line)
        self.assertTrue(lines[switch_index].endswith('M6T7'))
        self.assertEqual(lines[switch_index + 1], 'N' + str(10 * (switch_index + 1)) + 'G43H7')
        # the switch sits after the header, between the two bodies
        self.assertGreater(program.index('M6T7'), program.index('N60G43H2'))
        self.assertIn('G0Z', lines[switch_index - 1])  # previous line is piece A's retract

    def test_internal_tool_switch_updates_active_tool(self):
        piece_b = _patched_sample(extra_body='M6T7\nG43 H7\n')
        piece_c = _sample_mm()
        program = self._program([
            {'name': 'b', 'text': piece_b, 'place_x': 10, 'place_y': 10},
            {'name': 'c', 'text': piece_c, 'place_x': 600, 'place_y': 10},
        ])
        # after piece b's internal M6T7 the adapter must switch back to T2
        self.assertEqual(len(_lines_with(program, 'M6T2')), 2)  # header + switch back
        back_switch = _lines_with(program, 'G43H2')
        self.assertEqual(len(back_switch), 2)

    def test_first_piece_without_m141_is_blocked(self):
        with self.assertRaisesRegex(SheetAdapterError, 'M141'):
            self._program([{'name': 'a', 'text': _patched_sample(no_m141=True), 'place_x': 0, 'place_y': 0}])

    def test_output_uses_crlf_line_endings(self):
        program = self._program([{'name': 'a', 'text': _sample_mm(), 'place_x': 0, 'place_y': 0}])
        self.assertIn('\r\n', program)
        self.assertNotIn(' \r\n', program)
