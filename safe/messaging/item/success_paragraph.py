"""
InaSAFE Disaster risk assessment tool developed by AusAid - **Paragraph.**

Contact : ole.moller.nielsen@gmail.com

.. note:: This program is free software; you can redistribute it and/or modify
     it under the terms of the GNU General Public License as published by
     the Free Software Foundation; either version 2 of the License, or
     (at your option) any later version.
"""

__author__ = 'marco@opengis.ch'
__revision__ = '$Format:%H$'
__date__ = '27/05/2013'
__copyright__ = ('Copyright 2012, Australia Indonesia Facility for '
                 'Disaster Reduction')

from paragraph import Paragraph


class SuccessParagraph(Paragraph):
    """A Success Paragraph class for text blocks much like the p in html."""

    def to_html(self):
        """Render a Paragraph MessageElement as html

        Args:
            None

        Returns:
            Str the html representation of the Paragraph MessageElement

        Raises:
            Errors are propagated
        """
        if self.text is None:
            return
        else:
            return '<p class="alert alert-success">%s</p>' % self.text.to_html()

    def to_text(self):
        """Render a Paragraph MessageElement as plain text

        Args:
            None

        Returns:
            Str the plain text representation of the Paragraph MessageElement

        Raises:
            Errors are propagated
        """
        if self.text is None:
            return
        else:
            return "\nSUCCESS: %s\n" % self.text
