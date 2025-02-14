"""
Classes for spectral analysis.
"""

import datetime
from copy import copy
from math import floor
from random import randint
from distutils.version import LooseVersion
from typing import Union, List
import matplotlib
import numpy as np
from skimage import filters, morphology
from matplotlib import pyplot as plt
from matplotlib.colorbar import Colorbar
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, IndexLocator, MaxNLocator
from numpy import ma
from scipy import ndimage, signal
from sortedcontainers import SortedList
import ruptures as rpt

from sunpy import __version__
from sunpy.time import parse_time

from radiospectra.spectrum import Spectrum
from radiospectra.util import ConditionalDispatch, Parent, common_base, get_day, merge, to_signed

__all__ = ['Spectrogram', 'LinearTimeSpectrogram']

SUNPY_LT_1 = LooseVersion(__version__) < LooseVersion('1.0')

# 1080 because that usually is the maximum vertical pixel count on modern
# screens nowadays (2012).
DEFAULT_YRES = 1080

# This should not be necessary, as observations do not take more than a day
# but it is used for completeness' and extendibility's sake.
# XXX: Leap second?
SECONDS_PER_DAY = 86400

# Used for COPY_PROPERTIES
REFERENCE = 0
COPY = 1
DEEPCOPY = 2


def figure(*args, **kwargs):
    """
    Returns a new SpectroFigure, a figure extended with features useful for
    analysis of spectrograms.

    Compare pyplot.figure.
    """
    kw = {
        'FigureClass': SpectroFigure,
    }
    kw.update(kwargs)
    return plt.figure(*args, **kw)


def _min_delt(arr):
    deltas = (arr[:-1] - arr[1:])
    # Multiple values at the same frequency are just thrown away
    # in the process of linearizaion
    return deltas[deltas != 0].min()


def _list_formatter(lst, fun=None):
    """
    Returns a function that takes x, pos and returns fun(lst[x]) if fun is not
    None, else lst[x] or "" if x is out of range.
    """

    def _fun(x, pos):
        x = int(x)
        if x >= len(lst) or x < 0:
            return ""

        elem = lst[x]
        if fun is None:
            return elem
        return fun(elem)

    return _fun


def _union(sets):
    """
    Returns a union of sets.
    """
    union = set()
    for s in sets:
        union |= s
    return union


class _LinearView(object):
    """
    Helper class for frequency channel linearization.

    Attributes
    ----------
    arr : Spectrogram
        Spectrogram to linearize.
    delt : float
        Delta between frequency channels in linearized spectrogram. Defaults to
        (minimum delta / 2.) because of the Shannon sampling theorem.
    """

    def __init__(self, arr, delt=None):
        self.arr = arr
        if delt is None:
            # Nyquist–Shannon sampling theorem
            delt = _min_delt(arr.freq_axis) / 2.

        self.delt = delt

        midpoints = (self.arr.freq_axis[:-1] + self.arr.freq_axis[1:]) / 2
        self.midpoints = np.concatenate([midpoints, arr.freq_axis[-1:]])

        self.max_mp_delt = np.min(self.midpoints[1:] - self.midpoints[:-1])

        self.freq_axis = np.arange(
            self.arr.freq_axis[0], self.arr.freq_axis[-1], -self.delt
        )
        self.time_axis = self.arr.time_axis

        self.shape = (len(self), arr.data.shape[1])

    def __len__(self):
        return int(1 + (self.arr.freq_axis[0] - self.arr.freq_axis[-1]) /
                   self.delt)

    def _find(self, arr, item):
        if item < 0:
            item = item % len(self)
        if item >= len(self):
            raise IndexError

        freq_offset = item * self.delt
        freq = self.arr.freq_axis[0] - freq_offset
        # The idea is that when we take the biggest delta in the mid points,
        # we do not have to search anything that is between the beginning and
        # the first item that can possibly be that frequency.
        min_mid = int(max(0, (freq - self.midpoints[0]) // self.max_mp_delt))
        for n, mid in enumerate(self.midpoints[min_mid:]):
            if mid <= freq:
                return arr[min_mid + n]
        return arr[min_mid + n]

    def __getitem__(self, item):
        return self._find(self.arr, item)

    def get_freq(self, item):
        return self._find(self.arr.freq_axis, item)

    def make_mask(self, max_dist):
        mask = np.zeros(self.shape, dtype=np.bool)
        for n, item in enumerate(range(len(self))):
            freq = self.arr.freq_axis[0] - item * self.delt
            if abs(self.get_freq(item) - freq) > max_dist:
                mask[n, :] = True
        return mask


class SpectroFigure(Figure):
    def _init(self, data, freqs):
        self.data = data
        self.freqs = freqs

    def ginput_to_time(self, inp):
        return [
            self.data.start + datetime.timedelta(seconds=secs)
            for secs in self.ginput_to_time_secs(inp)
        ]

    def ginput_to_time_secs(self, inp):
        return np.array([float(self.data.time_axis[x]) for x, y in inp])

    def ginput_to_time_offset(self, inp):
        v = self.ginput_to_time_secs(inp)
        return v - v.min()

    def ginput_to_freq(self, inp):
        return np.array([self.freqs[y] for x, y in inp])

    def time_freq(self, points=0):
        inp = self.ginput(points)
        min_ = self.ginput_to_time_secs(inp).min()
        start = self.data.start + datetime.timedelta(seconds=min_)
        return TimeFreq(
            start, self.ginput_to_time_offset(inp), self.ginput_to_freq(inp)
        )


class TimeFreq(object):
    """
    Class to use for plotting frequency vs time.

    Attributes
    ----------
    start : `datetime.datetime`
        Start time of the plot.
    time : `~numpy.ndarray`
        Time of the data points as offset from start in seconds.
    freq : `~numpy.ndarray`
        Frequency of the data points in MHz.
    """

    def __init__(self, start, time, freq):
        self.start = start
        self.time = time
        self.freq = freq

    def plot(self, time_fmt="%H:%M:%S", **kwargs):
        """
        Plot the spectrum.

        Parameters
        ----------
        time_fmt : str
            The time format in a `~datetime.datetime` compatible format

        **kwargs : dict
            Any additional plot arguments that should be used
            when plotting.

        Returns
        -------
        fig : `~matplotlib.Figure`
            A plot figure.
        """
        figure = plt.gcf()
        axes = figure.add_subplot(111)
        axes.plot(self.time, self.freq, **kwargs)
        xa = axes.get_xaxis()
        xa.set_major_formatter(
            FuncFormatter(
                lambda x, pos: (
                    self.start + datetime.timedelta(seconds=x)
                ).strftime(time_fmt)
            )
        )

        axes.set_xlabel("Time [UT]")
        axes.set_ylabel("Frequency [MHz]")

        xa = axes.get_xaxis()
        for tl in xa.get_ticklabels():
            tl.set_fontsize(10)
            tl.set_rotation(30)
        figure.add_axes(axes)
        figure.subplots_adjust(bottom=0.2)
        figure.subplots_adjust(left=0.2)

        return figure

    def peek(self, show=True, *args, **kwargs):
        """
        Plot spectrum onto current axes.

        Parameters
        ----------
        show: bool
            if plot or nod the image. Default True
        *args : dict

        **kwargs : dict
            Any additional plot arguments that should be used
            when plotting.

        Returns
        -------
        fig : `~matplotlib.Figure`
            A plot figure.
        """
        plt.figure()
        ret = self.plot(*args, **kwargs)
        if show:
            plt.show()
        plt.close()
        return ret


class Spectrogram(Parent):
    """
    Spectrogram Class.

    .. warning:: This module is under development! Use at your own risk.

    Attributes
    ----------
    data : `~numpy.ndarray`
        two-dimensional array of the image data of the spectrogram.
    time_axis : `~numpy.ndarray`
        one-dimensional array containing the offset from the start
        for each column of data.
    freq_axis : `~numpy.ndarray`
        one-dimensional array containing information about the
        frequencies each row of the image corresponds to.
    start : `~datetime.datetime`
        starting time of the measurement
    end : `~datetime.datetime`
        end time of the measurement
    t_init : int
        offset from the start of the day the measurement began. If None
        gets automatically set from start.
    t_label : str
        label for the time axis
    f_label : str
        label for the frequency axis
    content : str
        header for the image
    instruments : str array
        instruments that recorded the data, may be more than one if
        it was constructed using combine_frequencies or join_many.
    """
    # Contrary to what pylint may think, this is not an old-style class.
    # pylint: disable=E1002,W0142,R0902

    # This needs to list all attributes that need to be
    # copied to maintain the object and how to handle them.
    COPY_PROPERTIES = [
        ('time_axis', COPY),
        ('freq_axis', COPY),
        ('instruments', COPY),
        ('start', REFERENCE),
        ('end', REFERENCE),
        ('t_label', REFERENCE),
        ('f_label', REFERENCE),
        ('content', REFERENCE),
        ('t_init', REFERENCE),
    ]
    _create = ConditionalDispatch.from_existing(Parent._create)

    @property
    def shape(self):
        return self.data.shape

    @property
    def dtype(self):
        return self.data.dtype

    @property
    def noise(self):
        arr = np.absolute(np.asanyarray(self.data))
        std = round(np.nanstd(arr), 3)
        return round(np.nanmean(arr) / std, 3)

    def _get_params(self):
        """
        Implementation detail.
        """
        return {
            name: getattr(self, name) for name, _ in self.COPY_PROPERTIES
        }

    def _slice(self, y_range, x_range):
        """
        Return new spectrogram reduced to the values passed as slices.

        Implementation detail.
        """
        data = self.data[y_range, x_range]
        params = self._get_params()

        soffset = 0 if x_range.start is None else x_range.start
        soffset = int(soffset)
        eoffset = self.shape[1] if x_range.stop is None else x_range.stop  # pylint: disable=E1101
        eoffset -= 1
        eoffset = int(eoffset)

        params.update({
            'time_axis': self.time_axis[
                         x_range.start:x_range.stop:x_range.step
                         ] - self.time_axis[soffset],
            'freq_axis': self.freq_axis[
                         y_range.start:y_range.stop:y_range.step],
            'start': self.start + datetime.timedelta(
                seconds=self.time_axis[soffset]),
            'end': self.start + datetime.timedelta(
                seconds=self.time_axis[eoffset]),
            't_init': self.t_init + self.time_axis[soffset],
        })
        return self.__class__(data, **params)

    def _with_data(self, data):
        new = copy(self)
        new.data = data
        return new

    def __apply_tophat__(self, data, disk):
        """
        Apply a morphological tophat transformation on the data

        Parameters
        ----------
        data : Numpy array.
            spec data transform into a numpy array
        disk : float
            Size of the disk for the morphology transformation
        """
        footprint = morphology.disk(disk)
        result = morphology.white_tophat(data, footprint)
        return data - result

    def __mask_data__(self, data):
        """
        Apply a mask to the spec data himself. The mask correspond to that values where the parameter
        data is equal to 0. The final result is the spec.data = spec.data ∩ data

        Parameters
        ----------
        data : Numpy array.
            data that will take it for get the mask
        """

        _mask = np.ma.masked_where(data == 0, data)
        original_masked = np.ma.array(self.data, mask=_mask.mask)
        return np.ma.filled(original_masked.astype(float), 0)

    def __find_peaks__(self, pdata, pmax=70, pmin=20, distance=100):
        """
        Apply an algebra procedure for find the highest peaks values in the data using percentile function

        Parameters
        ----------
        pdata : Numpy array.
            A copy of spec.data
        pmax : int.
            Max percentile
        pmin : int.
            Min percentile
        distance : int.
            Distance between the peaks

        Return
        ----------
        tuple
        """

        data = np.copy(pdata)
        for i in range(data.shape[0]):
            data[i, :] = data[i, :] - data[i, :].mean()

        datap = np.nan_to_num(data)
        p = np.percentile(datap, pmax, axis=0) - np.percentile(datap, pmin, axis=0)
        p = p - p.min()
        peaks, properties = signal.find_peaks(p, distance=distance)

        return p, peaks, p[peaks]

    def __init__(self, data, time_axis, freq_axis, start, end, t_init=None,
                 t_label="Time", f_label="Frequency", content="",
                 instruments=None):
        # Because of how object creation works, there is no avoiding
        # unused arguments in this case.
        self.data = data

        if t_init is None:
            diff = start - get_day(start)
            t_init = diff.seconds
        if instruments is None:
            instruments = set()

        self.start = start
        self.end = end

        self.t_label = t_label
        self.f_label = f_label

        self.t_init = t_init

        self.time_axis = time_axis
        self.freq_axis = freq_axis
        self.rfi_freq_axis = np.array([])

        self.content = content
        self.instruments = instruments

    def time_formatter(self, x, pos):
        """
        This returns the label for the tick of value x at a specified pos on
        the time axis.
        """
        # Callback, cannot avoid unused arguments.
        # pylint: disable=W0613
        x = int(x)
        if x >= len(self.time_axis) or x < 0:
            return ""
        return self.format_time(
            self.start + datetime.timedelta(
                seconds=float(self.time_axis[x])
            )
        )

    @staticmethod
    def format_time(time):
        """
        Override to configure default plotting.
        """
        return time.strftime("%H:%M:%S")

    @staticmethod
    def format_freq(freq):
        """
        Override to configure default plotting.
        """
        return "{freq:0.1f}".format(freq=freq)

    def peek(self, *args, **kwargs):
        """
        Plot spectrum onto current axes.

        Parameters
        ----------
        *args : dict

        **kwargs : dict
            Any additional plot arguments that should be used
            when plotting.

        Returns
        -------
        fig : `~matplotlib.Figure`
            A plot figure.
        """
        figure()
        ret = self.plot(*args, **kwargs)
        plt.show()
        return ret

    def plot(self, figure=None, overlays=[], colorbar=True, vmin=None,
             vmax=None, linear=True, showz=True, yres=DEFAULT_YRES,
             max_dist=None, nancolor='black', **matplotlib_args):
        """
        Plot spectrogram onto figure.

        Parameters
        ----------
        figure : `~matplotlib.Figure`
            Figure to plot the spectrogram on. If None, new Figure is created.
        overlays : list
            List of overlays (functions that receive figure and axes and return
            new ones) to be applied after drawing.
        colorbar : bool
            Flag that determines whether or not to draw a colorbar. If existing
            figure is passed, it is attempted to overdraw old colorbar.
        vmin : float
            Clip intensities lower than vmin before drawing.
        vmax : float
            Clip intensities higher than vmax before drawing.
        linear : bool
            If set to True, "stretch" image to make frequency axis linear.
        showz : bool
            If set to True, the value of the pixel that is hovered with the
            mouse is shown in the bottom right corner.
        yres : int or None
            To be used in combination with linear=True. If None, sample the
            image with half the minimum frequency delta. Else, sample the
            image to be at most yres pixels in vertical dimension. Defaults
            to 1080 because that's a common screen size.
        max_dist : float or None
            If not None, mask elements that are further than max_dist away
            from actual data points (ie, frequencies that actually have data
            from the receiver and are not just nearest-neighbour interpolated).
        nancolor: str
            compatible color with matplotlib for nan values
        """
        # [] as default argument is okay here because it is only read.
        # pylint: disable=W0102,R0914
        if linear:
            delt = yres
            if delt is not None:
                delt = max(
                    (self.freq_axis[0] - self.freq_axis[-1]) / (yres - 1),
                    _min_delt(self.freq_axis) / 2.
                )
                delt = float(delt)

            data = _LinearView(self.clip_values(vmin, vmax), delt)
            freqs = np.arange(
                self.freq_axis[0], self.freq_axis[-1], -data.delt
            )
        else:
            data = np.array(self.clip_values(vmin, vmax))
            freqs = self.freq_axis

        figure = plt.gcf()

        if figure.axes:
            axes = figure.axes[0]
        else:
            axes = figure.add_subplot(111)

        params = {
            'origin': 'lower',
            'aspect': 'auto',
        }
        params.update(matplotlib_args)
        if linear and max_dist is not None:
            toplot = ma.masked_array(data, mask=data.make_mask(max_dist))
        else:
            toplot = data

        current_cmap = matplotlib.cm.get_cmap()
        current_cmap.set_bad(color=nancolor)

        im = axes.imshow(toplot, **params)

        xa = axes.get_xaxis()
        ya = axes.get_yaxis()

        xa.set_major_formatter(
            FuncFormatter(self.time_formatter)
        )

        if linear:
            # Start with a number that is divisible by 5.
            init = (self.freq_axis[0] % 5) / data.delt
            nticks = 15.
            # Calculate MHz difference between major ticks.
            dist = (self.freq_axis[0] - self.freq_axis[-1]) / nticks
            # Round to next multiple of 10, at least ten.
            dist = max(round(dist, -1), 10)
            # One pixel in image space is data.delt MHz, thus we can convert
            # our distance between the major ticks into image space by dividing
            # it by data.delt.

            ya.set_major_locator(
                IndexLocator(
                    dist / data.delt, init
                )
            )
            ya.set_minor_locator(
                IndexLocator(
                    dist / data.delt / 10, init
                )
            )

            def freq_fmt(x, pos):
                # This is necessary because matplotlib somehow tries to get
                # the mid-point of the row, which we do not need here.
                x = x + 0.5
                return self.format_freq(self.freq_axis[0] - x * data.delt)
        else:
            freq_fmt = _list_formatter(freqs, self.format_freq)
            ya.set_major_locator(MaxNLocator(integer=True, steps=[1, 5, 10]))

        ya.set_major_formatter(
            FuncFormatter(freq_fmt)
        )

        axes.set_xlabel(self.t_label)
        axes.set_ylabel(self.f_label)
        # figure.suptitle(self.content)

        figure.suptitle(
            ' '.join([
                get_day(self.start).strftime("%d %b %Y"),
                'Radio flux density',
                '(' + ', '.join(self.instruments) + ')',
            ])
        )

        for tl in xa.get_ticklabels():
            tl.set_fontsize(10)
            tl.set_rotation(30)
        figure.add_axes(axes)
        figure.subplots_adjust(bottom=0.2)
        figure.subplots_adjust(left=0.2)

        if showz:
            axes.format_coord = self._mk_format_coord(
                data, figure.gca().format_coord)

        if colorbar:
            if len(figure.axes) > 1:
                Colorbar(figure.axes[1], im).set_label("Intensity")
            else:
                figure.colorbar(im).set_label("Intensity")

        for overlay in overlays:
            figure, axes = overlay(figure, axes)

        for ax in figure.axes:
            ax.autoscale()
        if isinstance(figure, SpectroFigure):
            figure._init(self, freqs)
        return axes

    def __getitem__(self, key):
        only_y = not isinstance(key, tuple)

        if only_y:
            return self.data[int(key)]
        elif isinstance(key[0], slice) and isinstance(key[1], slice):
            return self._slice(key[0], key[1])
        elif isinstance(key[1], slice):
            # return Spectrum( # XXX: Right class
            #     super(Spectrogram, self).__getitem__(key),
            #     self.time_axis[key[1].start:key[1].stop:key[1].step]
            # )
            return np.array(self.data[key])
        elif isinstance(key[0], slice):
            return Spectrum(
                self.data[key],
                self.freq_axis[key[0].start:key[0].stop:key[0].step]
            )

        return self.data[int(key)]

    def clip_freq(self, vmin=None, vmax=None):
        """
        Return a new spectrogram only consisting of frequencies in the interval
        [vmin, vmax].

        Parameters
        ----------
        vmin : float
            All frequencies in the result are greater or equal to this.
        vmax : float
            All frequencies in the result are smaller or equal to this.
        """
        left = 0
        if vmax is not None:
            while self.freq_axis[left] > vmax:
                left += 1

        right = len(self.freq_axis) - 1

        if vmin is not None:
            while self.freq_axis[right] < vmin:
                right -= 1

        return self[left:right + 1, :]

    def auto_find_background(self, amount=0.05):
        """
        Automatically find the background. This is done by first subtracting
        the average value in each channel and then finding those times which
        have the lowest standard deviation.

        Parameters
        ----------
        amount : float
            The percent amount (out of 1) of lowest standard deviation to
            consider.
        """
        # pylint: disable=E1101,E1103
        data = self.data.astype(to_signed(self.dtype))
        # Subtract average value from every frequency channel.
        tmp = (data - np.average(self.data, 1).reshape(self.shape[0], 1))
        # Get standard deviation at every point of time.
        # Need to convert because otherwise this class's __getitem__
        # is used which assumes two-dimensionality.
        sdevs = np.asarray(np.std(tmp, 0))

        # Get indices of values with lowest standard deviation.
        cand = sorted(list(range(self.shape[1])), key=lambda y: sdevs[y])
        # Only consider the best 5 %.
        return cand[:max(1, int(amount * len(cand)))]

    def auto_const_bg(self):
        """
        Automatically determine background.
        """
        realcand = self.auto_find_background()
        bg = np.average(self.data[:, realcand], 1)
        return bg.reshape(self.shape[0], 1)

    def subtract_bg(self, *args, **kwargs):
        """
        Perform background subtraction, with the opportunity to choose between different procedures
        and the ability to remove the radio frequency interference (RFI).

        default:
        Default background subtraction of radiospectra by using the "auto_const_bg()" function

        constbacksub:
        Background subtraction method where the average and the standard deviation of each row
        will be calculated and subtracted from the image.

        subtract_bg_sliding_window:
        Performs background subtraction with the possibility of having a sliding window and changing points.

        glid_back_sub:
        A gliding background subtraction method, where the sum weighted from the
        coefficients of the evenly spaces values of each row will be subtracted from the spectrogram.

        elimwrongchannels:
        Removing the RFI (radio frequency interference) from the spectrogram.

        Parameters
        ----------
        *args:
        List of desired methods that should be called.

        **kwargs:
        List of arguments that should be passed to a subfunction

        """
        spec = copy(self)
        for arg in args:
            if arg == "default":
                # default background subtraction of radiospectra
                spec = spec._with_data(spec.data - spec.auto_const_bg())

            elif arg == "constbacksub":
                spec = spec.constbacksub(overwrite=False)

            elif arg == "subtract_bg_sliding_window":
                _sbg, _bg, _min_sdevs, _cps = spec.subtract_bg_sliding_window(**kwargs)
                spec = _sbg

            elif arg == "glid_back_sub":
                spec = spec.glid_back_sub(overwrite=False)

            elif arg == "elimwrongchannels":
                spec = spec.elimwrongchannels(overwrite=False)

        if len(args) == 0:
            # default background subtraction of radiospectra
            spec = spec._with_data(spec.data - spec.auto_const_bg())

        return spec

    def subtract_bg_sliding_window(self, amount: float = 0.05, window_width: int = 0, affected_width: int = 0,
                                   change_points: Union[bool, List[int]] = False):
        """
        Performs background subtraction with a sliding window. Change points where significant jumps in
        value are observed or expected can be specified or automatically estimated. If change points are present
        each one will split the spectrogram and each resulting part will have its background removed independently.

        Parameters
        ----------
        amount : float
        The percent amount (out of 1) of lowest standard deviation to consider.

        window_width : int
        The width of the sliding window that is used to calculate the background.

        affected_width : int
        The width of the section where the background calculated by the window_width gets subtracted.
        It is centered in the sliding window and is also the step size.

        change_points : list of int or bool
        If a list of ints is provided it will use these values as change points. If a bool is
        provided it will estimate the change points if true or won't include any change points if false.
        """

        _data = self.data.copy()

        _og_image_height = _data.shape[0]
        _og_image_width = _data.shape[1]

        _bg = np.zeros([_og_image_height, _og_image_width])
        _min_sdevs = np.zeros([_og_image_height, _og_image_width])
        _out = _data.copy()

        if isinstance(change_points, bool):
            if change_points:
                _cps = self.estimate_change_points()
            else:
                _cps = []
        else:
            _cps = change_points

        if len(_cps) == 0:
            _images = [(0, _og_image_width)]
        else:
            _images = []
            _temp = 0
            _cps = sorted(_cps)
            for _cp in _cps:
                _images.append((_temp, _cp))
                _temp = _cp
            _images.append((_temp, _og_image_width))

        for (_img_start, _img_end) in _images:

            _cwp = _img_start

            _img_width = _img_end - _img_start
            _img_data = _data[:, _img_start:_img_end]

            _window_height = _og_image_height
            _window_width = _img_width if (window_width == 0 or window_width > _img_width) else window_width
            _affected_height = _og_image_height
            _affected_width = _img_width if (affected_width == 0 or affected_width > _img_width) else (
                affected_width if affected_width <= _window_width else _window_width)

            _data_minus_avg = (_img_data - np.average(_img_data, 1).reshape(_img_data.shape[0], 1))
            _sdevs = [(index, std) for (index, std) in enumerate(np.std(_data_minus_avg, 0))]

            _half = max((_window_width - _affected_width) // 2, 0)
            _division_fix = _half + _half != max(_img_width - _affected_width, 0)
            _max_amount = max(1, int(amount * _img_width))

            # calc initial set of used columns
            _window_sdevs = [sdev for sdev in _sdevs[:_half]]
            _sorted_sdevs = sorted(_window_sdevs, key=lambda y: y[1])
            _bg_used_sdevs = SortedList(_sorted_sdevs, key=lambda y: y[1])

            while _cwp <= _img_end:

                _affected_left = _cwp
                _affected_right = min(_affected_left + _affected_width, _img_end)
                _window_left = max(_affected_left - _half if _division_fix else _affected_left - _half, _img_start)
                _window_right = min(_affected_right + _half, _img_end)

                for sdev in _sdevs[max(_window_left - _affected_width, 0) - _img_start:_window_left - _img_start]:
                    _bg_used_sdevs.discard(sdev)

                if _window_right <= _img_end:
                    _bg_used_sdevs.update(
                        _sdevs[_window_right - _affected_width - _img_start:_window_right - _img_start])

                # calc current background
                _current_background = np.average(_img_data[:, [sdev[0] for sdev in _bg_used_sdevs[:_max_amount]]], 1)
                for sdev in _bg_used_sdevs[:_max_amount]:
                    _min_sdevs[:, sdev[0] + _img_start] += 1
                _bg[:, _affected_left:_affected_right] = np.repeat(_current_background.reshape(_bg.shape[0], 1),
                                                                   (_affected_right - _affected_left), axis=1)

                _cwp += _affected_width

        _sbg = np.ma.subtract(_out, _bg)
        return self._with_data(_sbg), self._with_data(_bg), self._with_data(_min_sdevs), _cps

    def estimate_change_points(self, window_width=100, max_length_single_segment=20000, segment_width=10000):
        """
        Estimates the change points of the spectrogram and returns the indices.
        If the spectrogram is too big it will get segmented.
        These segments will overlap by 2*window_width to not miss change points where the spectrogram is segmented.

        Parameters
        ----------
        window_width : int
            width of the sliding window
        max_length_single_segment : int
            max width of data array. If bigger it will get segmented because of memory reasons
        segment_width : int
            width of the segments if the CallistoSpectrogram gets segmented
        """
        avgs = np.average(self.data, axis=0)
        penalty = np.log(len(avgs)) * 8 * np.std(avgs) ** 2
        changepoints = set()

        if len(avgs) > max_length_single_segment:
            num = len(avgs) // (segment_width - window_width * 2)
            segments = [(x, x + segment_width) for x in np.multiply(range(0, num), (segment_width - window_width * 2))]
            segments.append((len(avgs) - segment_width, len(avgs)))
        else:
            segments = [(0, len(avgs))]

        if (len(segments)) > 3:
            m = 'mahalanobis'
        else:
            m = 'rbf'

        for start, end in segments:
            mod = rpt.Window(model=m, width=window_width).fit(avgs[start:end])
            res = np.array(mod.predict(pen=penalty)[:-1]) + start
            changepoints.update(res)
        return sorted(changepoints)

    def constbacksub(self, overwrite=True):
        """
        Background subtraction method where the average and the standard deviation of each row
        will be calculated and subtracted from the image.

        Parameters
        ----------
        overwrite : bool
            If function constbacksub has been called directly, there will be a possibility to overwrite it's current
            spectrogram data
        """
        im = self.data.copy()

        nx = im.shape[0]
        ny = im.shape[1]

        average_arr = np.average(im, axis=1)
        for i in range(nx):
            im[i, :] = im[i, :] - average_arr[i]

        sdev_arr = np.zeros(ny)
        for i in range(ny):
            sdev_arr[i] = np.std(im[:, i])

        zist = np.argsort(sdev_arr)
        nPart = int(ny * 0.05)
        zist = zist[0: nPart + 1]
        bkgArr = im[:, zist]

        background = np.average(bkgArr, axis=1)

        for j in range(ny):
            im[:, j] = im[:, j] - background

        if overwrite:
            self.data = im

        return self._with_data(im)

    def elimwrongchannels(self, overwrite=True):
        """
        Removing the RFI (radio frequency interference) from the spectrogram

        Parameters
        ----------
        overwrite : bool
            If function elimwrongchannels has been called directly, there will be a possibility to overwrite it's current
            spectrogram data
        """
        im = self.data.copy()
        freq_axis = self.freq_axis.copy()
        rfi_freq_axis = self.rfi_freq_axis.copy()
        ny = len(im.data[:, 0])

        # optimize std part
        std = np.ndarray((ny,), dtype=np.double)
        for i in range(ny):
            # Calculate std for each row and ignore nan's
            std[i] = np.nanstd(im[i, :].data)

        # Byte scale
        std = ((std - np.nanmin(std)) * 255) / (np.nanmax(std) - np.nanmin(std))
        std[std > 255] = 255
        np.nan_to_num(std, copy=False)
        std = std.astype(int)

        mean_sigma = np.average(std)
        positions = std < 5 * mean_sigma
        zist = std[positions]
        eliminated_channels = len(std) - len(zist)
        print(str(eliminated_channels) + " channels eliminated")

        rfi_freq_axis = self.update_rfi_header(freq_axis, rfi_freq_axis, positions)
        if np.sum(zist) != -1 and eliminated_channels != 0:
            im = im[positions, :]
            freq_axis = freq_axis[positions]

        print("Eliminating sharp jumps between channels ...")
        y_profile = np.average((filters.roberts(im.astype(float)).astype(float)), 1)
        masked_data = np.ma.masked_array(y_profile, np.isnan(y_profile))
        y_profile = masked_data - np.ma.average(masked_data)
        mean_sigma = np.std(y_profile)

        positions = np.abs(y_profile) < 2 * mean_sigma
        zist = y_profile[positions]
        eliminated_channels = len(y_profile) - len(zist)
        print(str(eliminated_channels) + " channels eliminated")

        rfi_freq_axis = self.update_rfi_header(freq_axis, rfi_freq_axis, positions)
        if np.sum(zist) != -1 and eliminated_channels != 0:
            im = im[positions, :]
            freq_axis = freq_axis[positions]

        if np.sum(zist) == -1:
            print("Sorry, all channels are bad ...")
            im = 0

        if overwrite:
            self.data = im
            self.freq_axis = freq_axis
            self.rfi_freq_axis = rfi_freq_axis

        spec = self._with_data(im)
        spec.freq_axis = freq_axis
        spec.rfi_freq_axis = rfi_freq_axis
        return spec

    def update_rfi_header(self, freq_axis, rfi_freq_axis, positions):
        for curr_position, valid in enumerate(positions):
            if not valid:
                curr_freq = freq_axis[curr_position]
                idx = rfi_freq_axis.searchsorted(curr_freq)
                rfi_freq_axis = np.concatenate((rfi_freq_axis[:idx], [curr_freq], rfi_freq_axis[idx:]))
        return rfi_freq_axis

    def glid_back_sub(self, window_width=0, weighted=False, overwrite=True):
        """
        A gliding background subtraction method, where the sum weighted from the
        coefficients of the evenly spaces values of each row will be subtracted from the spectrogram.

        Parameters
        ----------
        window_width: int
            The width of the sliding window that is used to calculate the background.
        weighted : bool
            If true, the coefficients of each rows will be considered dependent on the image width.
        overwrite : bool
            If function glid_back_sub has been called directly, there will be a possibility to overwrite it's current
            spectrogram data
        """
        image = self.data.copy()

        nx = len(image[0, :])
        ny = len(image[:, 0])

        if window_width == 0:
            w_len_half = int(image.shape[1] / 2)
        else:
            w_len_half = int(window_width / 2)
        w_len = w_len_half * 2

        backgr = np.zeros((ny, nx), dtype=np.float)

        # i is of type float!
        i = 0

        if weighted:
            coeffs = w_len_half - np.arange(0, w_len_half, dtype=np.float)
            sum = np.sum(coeffs)
            while i < w_len_half:
                img_data = image[:, 0:w_len_half + i].flatten()
                min_array_size = min(coeffs.size, img_data.size)
                coeffs_sum = np.multiply(img_data[0:min_array_size], coeffs[0:min_array_size])
                backgr[:, i] = np.sum(coeffs_sum) / sum

                img_data = image[:, nx - i - 1:nx].flatten()
                flipped_coeffs = coeffs[::-1]
                min_array_size = min(flipped_coeffs.size, img_data.size)
                coeffs_sum = np.multiply(img_data[0:min_array_size], flipped_coeffs[0:min_array_size])

                backgr[:, nx - i - 1] = np.sum(coeffs_sum) / sum
                i += 1
                coeffs = np.insert(coeffs, 0, w_len_half - i)
                sum = sum + coeffs[i]

            while i < nx - w_len_half:
                img_data = image[:, i - w_len_half:i + w_len_half].flatten()
                min_array_size = min(coeffs.size, img_data.size)
                coeffs_sum = np.multiply(img_data[0:min_array_size], coeffs[0:min_array_size])
                backgr[:, i] = np.sum(coeffs_sum) / sum
                i += 1
        else:
            while i < w_len_half + 1:
                backgr[:, i] = np.average(image[:, 0:min(i + w_len_half, nx)] + 1, 1)
                i += 1

            i = w_len_half + 1
            while i < nx - w_len_half:
                backgr[:, i] = backgr[:, i - 1] + (image[:, i + w_len_half] - image[:, i - 1 - w_len_half]) / w_len
                i += 1

            while i <= nx - 1:
                backgr[:, i] = np.average(image[:, i:nx], 1)
                i += 1

        if overwrite:
            self.data = image

        return self._with_data(image - backgr)

    def randomized_auto_const_bg(self, amount):
        """
        Automatically determine background. Only consider a randomly chosen
        subset of the image.

        Parameters
        ----------
        amount : int
            Size of random sample that is considered for calculation of
            the background.
        """
        cols = [randint(0, self.shape[1] - 1) for _ in range(amount)]

        # pylint: disable=E1101,E1103
        data = self.data.astype(to_signed(self.dtype))
        # Subtract average value from every frequency channel.
        tmp = (data - np.average(self.data, 1).reshape(self.shape[0], 1))
        # Get standard deviation at every point of time.
        # Need to convert because otherwise this class's __getitem__
        # is used which assumes two-dimensionality.
        tmp = tmp[:, cols]
        sdevs = np.asarray(np.std(tmp, 0))

        # Get indices of values with lowest standard deviation.
        cand = sorted(list(range(amount)), key=lambda y: sdevs[y])
        # Only consider the best 5 %.
        realcand = cand[:max(1, int(0.05 * len(cand)))]

        # Average the best 5 %
        bg = np.average(self[:, [cols[r] for r in realcand]], 1)

        return bg.reshape(self.shape[0], 1)

    def randomized_subtract_bg(self, amount):
        """
        Perform randomized constant background subtraction. Does not produce
        the same result every time it is run.

        Parameters
        ----------
        amount : int
            Size of random sample that is considered for calculation of
            the background.
        """
        return self._with_data(self.data - self.randomized_auto_const_bg(amount))

    def clip_values(self, vmin=None, vmax=None, out=None):
        """
        Clip intensities to be in the interval [vmin, vmax].

        Any values greater than the maximum will be assigned the maximum,
        any values lower than the minimum will be assigned the minimum.
        If either is left out or None, do not clip at that side of the interval.

        Parameters
        ----------
        min : int or float
            New minimum value for intensities.
        max : int or float
            New maximum value for intensities
        """
        # pylint: disable=E1101
        if vmin is None:
            vmin = int(np.nanmin(self.data))

        if vmax is None:
            vmax = int(np.nanmax(self.data))

        return self._with_data(self.data.clip(vmin, vmax, out))

    def rescale(self, vmin=0, vmax=1, dtype=np.dtype('float32')):
        """
        Rescale intensities to [vmin, vmax]. Note that vmin ≠ vmax and
        spectrogram.min() ≠ spectrogram.max().

        Parameters
        ----------
        vmin : float or int
            New minimum value in the resulting spectrogram.
        vmax : float or int
            New maximum value in the resulting spectrogram.
        dtype : `numpy.dtype`
            Data-type of the resulting spectrogram.
        """
        if vmax == vmin:
            raise ValueError("Maximum and minimum must be different.")
        if self.data.max() == self.data.min():
            raise ValueError("Spectrogram needs to contain distinct values.")
        data = self.data.astype(dtype)  # pylint: disable=E1101
        return self._with_data(
            vmin + (vmax - vmin) * (data - self.data.min()) /  # pylint: disable=E1101
            (self.data.max() - self.data.min())  # pylint: disable=E1101
        )

    def interpolate(self, frequency):
        """
        Linearly interpolate intensity at unknown frequency using linear
        interpolation of its two neighbours.

        Parameters
        ----------
        frequency : float or int
            Unknown frequency for which to linearly interpolate the intensities.
            freq_axis[0] >= frequency >= self_freq_axis[-1]
        """
        lfreq, lvalue = None, None
        for freq, value in zip(self.freq_axis, self.data[:, :]):
            if freq < frequency:
                break
            lfreq, lvalue = freq, value
        else:
            raise ValueError("Frequency not in interpolation range")
        if lfreq is None:
            raise ValueError("Frequency not in interpolation range")
        diff = frequency - freq  # pylint: disable=W0631
        ldiff = lfreq - frequency
        return (ldiff * value + diff * lvalue) / (diff + ldiff)  # pylint: disable=W0631

    def linearize_freqs(self, delta_freq=None):
        """
        Rebin frequencies so that the frequency axis is linear.

        Parameters
        ----------
        delta_freq : float
            Difference between consecutive values on the new frequency axis.
            Defaults to half of smallest delta in current frequency axis.
            Compare Nyquist-Shannon sampling theorem.
        """
        if delta_freq is None:
            # Nyquist–Shannon sampling theorem
            delta_freq = _min_delt(self.freq_axis) / 2.
        nsize = int((self.freq_axis.max() - self.freq_axis.min()) /
                    delta_freq + 1)
        new = np.zeros((int(nsize), self.shape[1]), dtype=self.data.dtype)

        freqs = self.freq_axis - self.freq_axis.max()
        freqs = freqs / delta_freq

        midpoints = np.round((freqs[:-1] + freqs[1:]) / 2)
        fillto = np.concatenate(
            [midpoints - 1, np.round([freqs[-1]]) - 1]
        )
        fillfrom = np.concatenate(
            [np.round([freqs[0]]), midpoints - 1]
        )

        fillto = np.abs(fillto)
        fillfrom = np.abs(fillfrom)

        for row, from_, to_ in zip(self, fillfrom, fillto):
            new[int(from_): int(to_)] = row

        vrs = self._get_params()
        vrs.update({
            'freq_axis': np.linspace(
                self.freq_axis.max(), self.freq_axis.min(), nsize
            )
        })

        return self.__class__(new, **vrs)

    def freq_overlap(self, other):
        """
        Get frequency range present in both spectrograms. Returns (min, max)
        tuple.

        Parameters
        ----------
        other : Spectrogram
            other spectrogram with which to look for frequency overlap
        """
        lower = max(self.freq_axis[-1], other.freq_axis[-1])
        upper = min(self.freq_axis[0], other.freq_axis[0])
        if lower > upper:
            raise ValueError("No overlap.")
        return lower, upper

    def time_to_x(self, time):
        """
        Return x-coordinate in spectrogram that corresponds to the passed
        `~datetime.datetime` value.

        Parameters
        ----------
        time : `~sunpy.time.parse_time` compatible str
            `~datetime.datetime` to find the x coordinate for.
        """
        diff = time - self.start
        diff_s = SECONDS_PER_DAY * diff.days + diff.seconds
        if self.time_axis[-1] < diff_s < 0:
            raise ValueError("Out of bounds")
        for n, elem in enumerate(self.time_axis):
            if diff_s < elem:
                return n - 1
        # The last element is the searched one.
        return n

    def at_freq(self, freq):
        return self[np.nonzero(self.freq_axis == freq)[0], :]

    @staticmethod
    def _mk_format_coord(spec, fmt_coord):
        def format_coord(x, y):
            shape = list(map(int, spec.shape))

            xint, yint = int(x), int(y)
            if 0 <= xint < shape[1] and 0 <= yint < shape[0]:
                pixel = spec[yint][xint]
            else:
                pixel = ""

            return '{!s} z={!s}'.format(fmt_coord(x, y), pixel)

        return format_coord

    def denoise(self, disk=3, full=False):
        """
        Denoise the data after apply bg_substraction and eliminwrongchannels.

        Parameters
        ----------
        disk : float.
            Size of the disk for the morphology transformation.
        full : bool.
            If apply more denoise after the morphological transformation.

        Return
        ----------
        object
        """

        spec = copy(self)
        data = np.array(spec.data)
        data[data < 0] = 0
        data = self.__apply_tophat__(data, disk)

        if full:
            auxdata = data.copy()
            p, peaks, values = self.__find_peaks__(auxdata)

            if len(peaks) >= 4:
                max_peak_pos = np.argmax(values)
                l, r = peaks[max_peak_pos - 1], peaks[max_peak_pos + 1]
                p[:l] = 0
                p[r:] = 0

                if p.max() == 0:
                    auxdata = self.__apply_tophat__(auxdata, disk=disk)
                    p, peaks, values = self.__find_peaks__(auxdata)
                    max_peak_pos = np.argmax(values)
                    l, r = peaks[max_peak_pos - 1], peaks[max_peak_pos + 1]
                    p[:l] = 0
                    p[r:] = 0

                # Clean more the image on t3
                if len(peaks) >= 4:
                    max_peak_pos = np.argmax(p[peaks])
                    l, r = peaks[max_peak_pos - 1], peaks[max_peak_pos + 1]

                auxdata[:, :l] = 0
                auxdata[:, r:] = 0
                data = auxdata

        spec.data = self.__mask_data__(data)
        return spec


class LinearTimeSpectrogram(Spectrogram):
    """
    Spectrogram evenly sampled in time.

    Attributes
    ----------
    t_delt : float
        difference between the items on the time axis
    """
    # pylint: disable=E1002
    COPY_PROPERTIES = Spectrogram.COPY_PROPERTIES + [
        ('t_delt', REFERENCE),
    ]

    def __init__(self, data, time_axis, freq_axis, start, end,
                 t_init=None, t_delt=None, t_label="Time", f_label="Frequency",
                 content="", instruments=None):
        if t_delt is None:
            t_delt = _min_delt(freq_axis)

        super(LinearTimeSpectrogram, self).__init__(
            data, time_axis, freq_axis, start, end, t_init, t_label, f_label,
            content, instruments
        )
        self.t_delt = t_delt

    @staticmethod
    def make_array(shape, dtype=np.dtype('float32')):
        """
        Function to create an array with shape and dtype.

        Parameters
        ----------
        shape : tuple
            shape of the array to create
        dtype : `numpy.dtype`
            data-type of the array to create
        """
        return np.zeros(shape, dtype=dtype)

    @staticmethod
    def memmap(filename):
        """
        Return function that takes shape and dtype and returns a memory mapped
        array.

        Parameters
        ----------
        filename : str
            File to store the memory mapped array in.
        """
        return (
            lambda shape, dtype=np.dtype('float32'): np.memmap(
                filename, mode="write", shape=shape, dtype=dtype
            )
        )

    def resample_time(self, new_delt):
        """
        Rescale image so that the difference in time between pixels is new_delt
        seconds.

        Parameters
        ----------
        new_delt : float
            New delta between consecutive values.
        """
        if self.t_delt == new_delt:
            return self
        factor = self.t_delt / float(new_delt)

        # The last data-point does not change!
        new_size = floor((self.shape[1] - 1) * factor + 1)  # pylint: disable=E1101
        data = ndimage.zoom(self.data, (1, new_size / self.shape[1]))  # pylint: disable=E1101

        params = self._get_params()
        params.update({
            'time_axis': np.linspace(
                self.time_axis[0],
                self.time_axis[int((new_size - 1) * new_delt / self.t_delt)],
                new_size
            ),
            't_delt': new_delt,
        })
        return self.__class__(data, **params)

    JOIN_REPEAT = object()

    @classmethod
    def join_many(cls, specs, mk_arr=None, nonlinear=False,
                  maxgap=0, fill=JOIN_REPEAT):
        """
        Produce new Spectrogram that contains spectrograms joined together in
        time.

        Parameters
        ----------
        specs : list
            List of spectrograms to join together in time.
        nonlinear : bool
            If True, leave out gaps between spectrograms. Else, fill them with
            the value specified in fill.
        maxgap : float, int or None
            Largest gap to allow in second. If None, allow gap of arbitrary
            size.
        fill : float or int
            Value to fill missing values (assuming nonlinear=False) with.
            Can be LinearTimeSpectrogram.JOIN_REPEAT to repeat the values for
            the time just before the gap.
        mk_array: function
            Function that is called to create the resulting array. Can be set
            to LinearTimeSpectrogram.memap(filename) to create a memory mapped
            result array.
        """
        # XXX: Only load header and load contents of files
        # on demand.
        mask = None

        if mk_arr is None:
            mk_arr = cls.make_array

        specs = sorted(specs, key=lambda x: x.start)

        freqs = specs[0].freq_axis
        if not all(np.array_equal(freqs, sp.freq_axis) for sp in specs):
            raise ValueError("Frequency channels do not match.")

        # Smallest time-delta becomes the common time-delta.
        min_delt = min(sp.t_delt for sp in specs)
        dtype_ = max(sp.dtype for sp in specs)

        specs = [sp.resample_time(min_delt) for sp in specs]
        size = sum(sp.shape[1] for sp in specs)

        data = specs[0]
        start_day = data.start

        xs = []
        last = data
        for elem in specs[1:]:
            e_init = (
                SECONDS_PER_DAY * (
                get_day(elem.start) - get_day(start_day)
            ).days + elem.t_init
            )
            x = int((e_init - last.t_init) / min_delt)
            xs.append(x)
            diff = last.shape[1] - x

            if maxgap is not None and -diff > maxgap / min_delt:
                raise ValueError("Too large gap.")

            # If we leave out undefined values, we do not want to
            # add values here if x > t_res.
            if nonlinear:
                size -= max(0, diff)
            else:
                size -= diff

            last = elem

        # The non existing element after the last one starts after
        # the last one. Needed to keep implementation below sane.
        xs.append(specs[-1].shape[1])

        # We do that here so the user can pass a memory mapped
        # array if they'd like to.
        arr = mk_arr((data.shape[0], size), dtype_)
        time_axis = np.zeros((size,))
        sx = 0
        # Amount of pixels left out due to non-linearity. Needs to be
        # considered for correct time axes.
        sd = 0
        for x, elem in zip(xs, specs):
            diff = x - elem.shape[1]
            e_time_axis = elem.time_axis

            elem = elem.data

            if x > elem.shape[1]:
                if nonlinear:
                    x = elem.shape[1]
                else:
                    # If we want to stay linear, fill up the missing
                    # pixels with placeholder zeros.
                    filler = np.zeros((data.shape[0], diff))
                    if fill is cls.JOIN_REPEAT:
                        filler[:, :] = elem[:, -1, np.newaxis]
                    else:
                        filler[:] = fill
                    minimum = e_time_axis[-1]
                    e_time_axis = np.concatenate([
                        e_time_axis,
                        np.linspace(
                            minimum + min_delt,
                            minimum + diff * min_delt,
                            diff
                        )
                    ])
                    elem = np.concatenate([elem, filler], 1)
            arr[:, sx:sx + x] = elem[:, :x]

            if diff > 0:
                if mask is None:
                    mask = np.zeros((data.shape[0], size), dtype=np.uint8)
                mask[:, sx + x - diff:sx + x] = 1
            time_axis[sx:sx + x] = e_time_axis[:x] + data.t_delt * (sx + sd)
            if nonlinear:
                sd += max(0, diff)
            sx += x
        params = {
            'time_axis': time_axis,
            'freq_axis': data.freq_axis,
            'start': data.start,
            'end': specs[-1].end,
            't_delt': data.t_delt,
            't_init': data.t_init,
            't_label': data.t_label,
            'f_label': data.f_label,
            'content': data.content,
            'instruments': _union(spec.instruments for spec in specs),
        }
        if mask is not None:
            arr = ma.array(arr, mask=mask)
        if nonlinear:
            del params['t_delt']
            return Spectrogram(arr, **params)
        return common_base(specs)(arr, **params)

    def time_to_x(self, time):
        """
        Return x-coordinate in spectrogram that corresponds to the passed
        datetime value.

        Parameters
        ----------
        time : `~sunpy.time.parse_time` compatible str
            `datetime.datetime` to find the x coordinate for.
        """
        # This is impossible for frequencies because that mapping
        # is not injective.
        if SUNPY_LT_1:
            time = parse_time(time)
        else:
            time = parse_time(time).datetime
        diff = time - self.start
        diff_s = SECONDS_PER_DAY * diff.days + diff.seconds
        result = diff_s // self.t_delt
        if 0 <= result <= self.shape[1]:  # pylint: disable=E1101
            return result
        raise ValueError("Out of range.")

    @staticmethod
    def intersect_time(specs):
        """
        Return slice of spectrograms that is present in all of the ones passed.

        Parameters
        ----------
        specs : list
            List of spectrograms of which to find the time intersections.
        """
        delt = min(sp.t_delt for sp in specs)
        start = max(sp.t_init for sp in specs)

        # XXX: Could do without resampling by using
        # sp.t_init below, not sure if good idea.
        specs = [sp.resample_time(delt) for sp in specs]
        cut = [sp[:, int((start - sp.t_init) / delt):] for sp in specs]

        length = min(sp.shape[1] for sp in cut)
        return [sp[:, :length] for sp in cut]

    @classmethod
    def combine_frequencies(cls, specs):
        """
        Return new spectrogram that contains frequencies from all the
        spectrograms in spec. Only returns time intersection of all of them.

        Parameters
        ----------
        spec : list
            List of spectrograms of which to combine the frequencies into one.
        """
        if not specs:
            raise ValueError("Need at least one spectrogram.")

        specs = cls.intersect_time(specs)

        one = specs[0]

        dtype_ = max(sp.dtype for sp in specs)
        fsize = sum(sp.shape[0] for sp in specs)

        new = np.zeros((fsize, one.shape[1]), dtype=dtype_)

        freq_axis = np.zeros((fsize,))

        for n, (data, row) in enumerate(merge(
            [
                [(sp, n) for n in range(sp.shape[0])] for sp in specs
            ],
            key=lambda x: x[0].freq_axis[x[1]]
        )):
            new[n, :] = data[row, :]
            freq_axis[n] = data.freq_axis[row]
        params = {
            'time_axis': one.time_axis,  # Should be equal
            'freq_axis': freq_axis,
            'start': one.start,
            'end': one.end,
            't_delt': one.t_delt,
            't_init': one.t_init,
            't_label': one.t_label,
            'f_label': one.f_label,
            'content': one.content,
            'instruments': _union(spec.instruments for spec in specs)
        }
        return common_base(specs)(new, **params)

    def check_linearity(self, err=None, err_factor=None):
        """
        Check linearity of time axis. If err is given, tolerate absolute
        derivation from average delta up to err. If err_factor is given,

        tolerate up to err_factor * average_delta. If both are given,
        TypeError is raised. Default to err=0.

        Parameters
        ----------
        err : float
            Absolute difference each delta is allowed to diverge from the
            average. Cannot be used in combination with err_factor.
        err_factor : float
            Relative difference each delta is allowed to diverge from the
            average, i.e. err_factor * average. Cannot be used in combination
            with err.
        """
        deltas = self.time_axis[:-1] - self.time_axis[1:]
        avg = np.average(deltas)
        if err is None and err_factor is None:
            err = 0
        elif err is None:
            err = abs(err_factor * avg)
        elif err_factor is not None:
            raise TypeError("Only supply err or err_factor, not both")
        return (abs(deltas - avg) <= err).all()

    def in_interval(self, start=None, end=None):
        """
        Return part of spectrogram that lies in [start, end).

        Parameters
        ----------
        start : None or `~datetime.datetime` or `~sunpy.time.parse_time` compatible string or time string
            Start time of the part of the spectrogram that is returned. If the
            measurement only spans over one day, a colon separated string
            representing the time can be passed.
        end : None or `~datetime.datetime` or `~sunpy.time.parse_time` compatible string or time string
            See start.
        """
        if start is not None:
            try:
                if SUNPY_LT_1:
                    start = parse_time(start)
                else:
                    start = parse_time(start).datetime
            except ValueError:
                # XXX: We could do better than that.
                if get_day(self.start) != get_day(self.end):
                    raise TypeError(
                        "Time ambiguous because data spans over more than one day"
                    )
                start = datetime.datetime(
                    self.start.year, self.start.month, self.start.day,
                    *list(map(int, start.split(":")))
                )
            start = self.time_to_x(start)
        if end is not None:
            try:
                if SUNPY_LT_1:
                    end = parse_time(end)
                else:
                    end = parse_time(end).datetime
            except ValueError:
                if get_day(self.start) != get_day(self.end):
                    raise TypeError(
                        "Time ambiguous because data spans over more than one day"
                    )
                end = datetime.datetime(
                    self.start.year, self.start.month, self.start.day,
                    *list(map(int, end.split(":")))
                )
            end = self.time_to_x(end)
        if start:
            start = int(start)
        if end:
            end = int(end)
        return self[:, start:end]
