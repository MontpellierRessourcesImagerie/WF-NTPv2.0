import numpy as np
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from multiprocessing import Pool, cpu_count
from scipy import interpolate, ndimage
import scipy.misc
import cv2
import cv2.cv as cv
import os
import time
import sys
import mahotas as mh
import pandas as pd
import trackpy as tp
from skimage import measure, morphology, io
from math import factorial
import random
import time
from skimage.transform import hough_circle
from skimage.feature import peak_local_max, canny
import skimage.draw
import cPickle
import warnings
import matplotlib.path as mplPath
from collections import defaultdict, Counter
from skimage.transform import resize

if len(sys.argv) == 1:
	from settings import *
	import shutil
	save_as = save_as.rstrip('/')+'/'
	try:
		os.mkdir(save_as)
	except OSError:
		pass
	shutil.copyfile('settings.py','%ssettings.py'%save_as)
else:
	settings_filename = sys.argv[1]
	with open(settings_filename) as f:
		for line in f:
			exec line
try: # Backwards compability
	minimum_ecc
except NameError:
	minimum_ecc = 0
try: # Backwards compability
	skeletonize
except NameError:
	skeletonize = False
	prune_size = 0
try: # Backwards compability
	do_full_prune
except:
	do_full_prune = False

frames_to_estimate_velocity = min([frames_to_estimate_velocity,
								   min_track_length])
bend_threshold /= 100.
parallel = False
videoname = filename
if not os.path.exists(videoname):
	print videoname, 'does not exist.'
	exit()

plt.figure(figsize=fig_size)

class Video:
	def __init__(self,fname,grey=False):
		self.cap = cv2.VideoCapture(fname)
		self.fname = fname
		self.name = "".join(fname.split(".")[:-1]).replace('/','_')
		self.len = self.cap.get(cv.CV_CAP_PROP_FRAME_COUNT) - start_frame
		if limit_images_to and limit_images_to<(self.len-start_frame):
			self.len = limit_images_to
		self.grey = grey
		if grey:
			for _ in xrange(100):
				ret, frame = self.cap.read()
				if ret:
					break
			if len(frame.shape)==2:
				self.grey = False
			self.cap.set(cv.CV_CAP_PROP_POS_FRAMES,0)
	def next(self):
		ret = False
		for _ in xrange(100):
			ret, frame = self.cap.read()
			if ret:
				break
			time.sleep(0.1*random.random())
		if ret:
			if self.grey:
				return frame[:,:,0]
			else:
				return frame
		else:
			raise StopIteration
	def set_index(self,i):
		self.cap.set(cv.CV_CAP_PROP_POS_FRAMES,i)
	def restart(self):
		self.set_index(start_frame)
	def __getitem__(self,i):
		if i<0:
			i += self.len
		self.set_index(start_frame+i)
		return self.next()
	def __len__(self):
		return int(self.len)
	def release(self):
		self.cap.release()
video = Video(videoname,grey=True)

print 'Video shape:', video[0].shape

region_shapes = {}
try:
	len(regions)
except:
	regions = {}
if len(regions)==0:
	im = np.ones_like(video[0])
	regions["all"] = im>0.1
	all_regions = im>0.1
else:
	all_regions = np.zeros_like(video[0])
	for key, d in regions.items():
		im = np.zeros_like(video[0])
		rr, cc = skimage.draw.polygon(np.array(d['y']),np.array(d['x']))
		try:
			im[rr,cc] = 1
		except IndexError:
			print 'Region "', key, '" cannot be applied to video', videoname
			print 'Input image sizes do not match.'
			exit()
		all_regions += im
		region_shapes[key] = im>0.1
	all_regions = all_regions>0.1

def savitzky_golay(y, window_size, order, deriv=0, rate=1):
	try:
		window_size = np.abs(np.int(window_size))
		order = np.abs(np.int(order))
	except ValueError, msg:
		raise ValueError("window_size and order have to be of type int")
	if window_size % 2 != 1 or window_size < 1:
		raise TypeError("window_size size must be a positive odd number")
	if window_size < order + 2:
		raise TypeError("window_size is too small for the polynomials order")
	order_range = range(order+1)
	half_window = (window_size -1) // 2
	# precompute coefficients
	b = np.mat([[k**i for i in order_range] for k in range(-half_window, half_window+1)])
	m = np.linalg.pinv(b).A[deriv] * rate**deriv * factorial(deriv)
	# pad the signal at the extremes with
	# values taken from the signal itself
	firstvals = y[0] - np.abs( y[1:half_window+1][::-1] - y[0] )
	lastvals = y[-1] + np.abs(y[-half_window-1:-1][::-1] - y[-1])
	y = np.concatenate((firstvals, y, lastvals))
	return np.convolve( m[::-1], y, mode='valid')

def find_Z(i0,i1):
	# Adjust brightness:
	frame = video[(i0+i1)//2]
	mean_brightness = np.mean(frame)
	if mean_brightness>1:
		mean_brightness /= 255.
	Z = np.zeros_like(frame,dtype=np.float64)
	if darkfield:
		minv = np.zeros_like(frame,dtype=np.float64)+256
	else:
		minv = np.zeros_like(frame,dtype=np.float64)-256
	for i in xrange(i0,i1,Z_skip_images):
		frame = video[i]
		frame = frame*mean_brightness/np.mean(frame)
		diff = frame
		if darkfield:
			logger = diff<minv
		else:
			logger = diff>minv
		minv[logger] = diff[logger]
		Z[logger] = frame[logger]
	return Z, mean_brightness

def find_Z_withdead(i0,i1):
	frame = video[(i0+i1)//2]
	Y,X = np.meshgrid(np.arange(frame.shape[1]),
						  np.arange(frame.shape[0]))
	thres = cv2.adaptiveThreshold(frame,1,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,\
            cv2.THRESH_BINARY,2*(std_px//2)+1,0)<0.5
	vals = frame[~thres]
	x = X[~thres]
	y = Y[~thres]
	Z = interpolate.griddata((x,y),vals,(X,Y),method='nearest')
	return Z, False

def small_imshow(img, *args, **kwargs):
	# For large images/frames matplotlib's imshow gives memoryerror
	# This is solved by resizing before plotting
	s = img.shape
	b = img
	if (s[0]+s[1])/2. > 1500:
		factor = 1500/((s[0]+s[1])/2.)
		b = resize(img, (int(s[0]*factor), int(s[1]*factor)),
					preserve_range=True)
	plt.clf()
	plt.imshow(b, *args, extent=[0,s[1],s[0],0], **kwargs)

def output_processing_frames(frameorig, Z, frame, thresholded,
			frame_after_open, frame_after_close, labeled, labeled_removed,
			skel_labeled=None):
	small_imshow(frameorig,cmap=cm.gray)
	plt.savefig('%s0frameorig.jpg'%save_as)

	small_imshow(Z,cmap=cm.gray)
	plt.savefig('%s0z.jpg'%save_as)

	small_imshow(frame,cmap=cm.gray)
	plt.savefig('%s1framesubtract.jpg'%save_as)

	small_imshow(thresholded,cmap=cm.binary)
	plt.savefig('%s2thresholded.jpg'%save_as)

	small_imshow(frame_after_open,cmap=cm.binary)
	plt.savefig('%s3opened.jpg'%save_as)

	small_imshow(frame_after_close,cmap=cm.binary)
	plt.savefig('%s4closed.jpg'%save_as)

	small_imshow(labeled,cmap=cm.jet)
	plt.savefig('%s5labelled.jpg'%save_as)

	small_imshow(labeled_removed,cmap=cm.jet)
	plt.savefig('%s6removed.jpg'%save_as)

	if skel_labeled is not None:
		small_imshow(skel_labeled,cmap=cm.jet)
		plt.savefig('%s7skeletonized.jpg'%save_as)
	plt.clf()

skel_endpoints1 = np.array([[0, 0, 0], [0, 1, 0], [2, 1, 2]])
skel_endpoints2 = np.array([[0, 0, 0], [0, 1, 2], [0, 2, 1]])
skel_endpoints3 = np.array([[0, 0, 2], [0, 1, 1], [0, 0, 2]])
skel_endpoints4 = np.array([[0, 2, 1], [0, 1, 2], [0, 0, 0]])
skel_endpoints5 = np.array([[2, 1, 2], [0, 1, 0], [0, 0, 0]])
skel_endpoints6 = np.array([[1, 2, 0], [2, 1, 0], [0, 0, 0]])
skel_endpoints7 = np.array([[2, 0, 0], [1, 1, 0], [2, 0, 0]])
skel_endpoints8 = np.array([[0, 0, 0], [2, 1, 0], [1, 2, 0]])
def find_skel_endpoints(skel):
    ep1 = mh.morph.hitmiss(skel, skel_endpoints1)
    ep2 = mh.morph.hitmiss(skel, skel_endpoints2)
    ep3 = mh.morph.hitmiss(skel, skel_endpoints3)
    ep4 = mh.morph.hitmiss(skel, skel_endpoints4)
    ep5 = mh.morph.hitmiss(skel, skel_endpoints5)
    ep6 = mh.morph.hitmiss(skel, skel_endpoints6)
    ep7 = mh.morph.hitmiss(skel, skel_endpoints7)
    ep8 = mh.morph.hitmiss(skel, skel_endpoints8)
    ep = ep1 + ep2 + ep3 + ep4 + ep5 + ep6 + ep7 + ep8
    return ep

def prune(skel, size):
    for _ in xrange(size):
        endpoints = find_skel_endpoints(skel)
        skel = np.logical_and(skel, np.logical_not(endpoints))
    return skel

def prune_fully(skel_labeled):
    for k in xrange(1000):
        endpoints = find_skel_endpoints(skel_labeled>0)>0
        idx = np.argwhere(endpoints)
        reg = skel_labeled[idx[:,0],idx[:,1]]
        count = Counter(reg)
        idx = np.array([idx[i,:] for i in xrange(len(reg))
                        if count[reg[i]]>2])
        if len(idx)==0:
            break
        endpoints[:] = 1
        endpoints[idx[:,0], idx[:,1]] = 0
        skel_labeled *= endpoints
    return skel_labeled

def apply_Z(i0,i1,Z,mean_brightness):
	"""Thresholds images with indeces i0 to i1 by using Z"""
	results = []
	for i in xrange(i0,i1):
		print ' : Locating in frame %i/%i'\
                    %(i+1+start_frame,len(video)+start_frame)
		frameorig = video[i]

		if mean_brightness:
			frame = frameorig*mean_brightness/np.mean(frameorig)
		else:
			frame = np.array(frameorig,dtype=np.float64)
		frame = np.abs(frame - Z) * all_regions
		if (frame>1.1).any():
			frame /= 255.

		thresholded = frame >(threshold/255.)

		if opening>0:
			frame_after_open = ndimage.binary_opening(thresholded, structure=np.ones((opening, opening))).astype(np.int)
		else:
			frame_after_open = thresholded

		if closing>0:
			frame_after_close = ndimage.binary_closing(frame_after_open, structure=np.ones((closing, closing))).astype(np.int)
		else:
			frame_after_close = frame_after_open


		labeled,_ = mh.label(frame_after_close, np.ones((3,3), bool))
		sizes = mh.labeled.labeled_size(labeled)

		remove = np.where(np.logical_or(sizes < min_size,
								sizes > max_size))
		labeled_removed = mh.labeled.remove_regions(labeled, remove)
		labeled_removed, n_left = mh.labeled.relabel(labeled_removed)


		props = measure.regionprops(labeled_removed)
		prop_list = [{"area":props[j].area,"centroid":props[j].centroid,
						"eccentricity":props[j].eccentricity,
						"area_eccentricity":props[j].eccentricity,
                        "minor_axis_length":props[j].minor_axis_length /
                        					(props[j].major_axis_length+0.001)}
						for j in xrange(len(props))]
		if skeletonize:
			skeletonized_frame = morphology.skeletonize(frame_after_close)
			skeletonized_frame = prune(skeletonized_frame, prune_size)

			skel_labeled = labeled_removed * skeletonized_frame
			if do_full_prune:
				skel_labeled = prune_fully(skel_labeled)

			skel_props = measure.regionprops(skel_labeled)
			for j in xrange(len(skel_props)):
				prop_list[j]["length"] = skel_props[j].area
				prop_list[j]["eccentricity"] = skel_props[j].eccentricity
				prop_list[j]["minor_axis_length"] = \
									 skel_props[j].minor_axis_length\
									/(skel_props[j].major_axis_length+0.001)

		results.append(prop_list)

		if i==0:
			print 'Sizes:'
			print sizes

			output_processing_frames(frameorig, Z, frame, thresholded,
				frame_after_open, frame_after_close, labeled, labeled_removed,
				(skel_labeled if skeletonize else None))
			print 'Example frame outputted!'

			if stop_after_example_output:
				return
		if i<output_overlayed_images or output_overlayed_images==None:
			warnings.filterwarnings("ignore")
			io.imsave('%s/imgs/%05d.jpg'%(save_as,i),
				np.array(labeled_removed==0,dtype=np.float32))
			warnings.filterwarnings("default")

	return results

def locate(args):
	i,zi = args
	if keep_dead_method:
		Z,mean_brightness = find_Z_withdead(*zi)
	else:
		Z,mean_brightness = find_Z(*zi)
	return apply_Z(*i,Z=Z,mean_brightness=mean_brightness)

def track_all_locations():
	apply_indeces = map(int,list(np.linspace(0,len(video),len(video)//use_images+2)))
	apply_indeces = zip(apply_indeces[:-1],apply_indeces[1:])
	Z_indeces = [(max([0,i-use_around]),min(j+use_around,len(video))) for i,j in apply_indeces]
	t0 =  time.time()
	args = zip(apply_indeces,Z_indeces)
	if parallel and not stop_after_example_output:
		p = Pool(cpu_count())
		split_results = p.map(locate,args,chunksize=1)
	else:
		if stop_after_example_output:
			locate(args[0])
			exit()
		else:
			split_results = map(locate,args)
	locations = []
	for l in split_results:
		locations += l
	return locations

def form_trajectories(loc):
	global particles,P,T,bends,track
	print
	print 'Forming worm trajectories...',
	data = {'x':[],'y':[],'frame':[],
			'eccentricity':[],'area':[],
            'minor_axis_length':[],
            'area_eccentricity':[]}
	for t,l in enumerate(loc):
		data['x'] += [d['centroid'][0] for d in l]
		data['y'] += [d['centroid'][1] for d in l]
		data['eccentricity'] += [d['eccentricity'] for d in l]
		data['area_eccentricity'] += [d['area_eccentricity'] for d in l]
		data['minor_axis_length'] += [d['minor_axis_length'] for d in l]
		data['area'] += [d['area'] for d in l]
		data['frame'] += [t]*len(l)
	data = pd.DataFrame(data)
	try:
		track = tp.link_df(data,search_range=max_dist_move,memory=memory)
	except tp.linking.SubnetOversizeException:
		print 'Linking problem too complex. Reduce maximum move distance or memory.'
		print 'Stopping.'
		exit()
	track = tp.filter_stubs(track, min([min_track_length,len(loc)]))
	try:
		trackfile = open('%strack.p'%save_as,'w')
		cPickle.dump(track, trackfile)
		trackfile.close()
	except:
		print 'Warning: no track file saved. Track too long.'
		print '         plot_path.py will not work on this file.'

	return track

def check_for_worms(particles):
	if len(particles)==0:
		f = open('%s/results.txt'%save_as,'w')
		f.write('---------------------------------\n')
		f.write('    Results for %s \n'%videoname)
		f.write('---------------------------------\n\n')
		f.write('No worms detected. Check your settings.\n\n')
		f.close()
		print 'No worms detected. Stopping!'
		exit()

def make_region_paths(regions):
	reg_paths = {}
	for key, d in regions.items():
		reg_paths[key] = mplPath.Path(np.array(zip(d['x']+[d['x'][0]],
					d['y']+[d['y'][0]])))
	return reg_paths

def identify_region(xs, ys, reg_paths):
	for x, y in zip(xs,ys):
		for key, path in reg_paths.items():
			if path.contains_point((y,x)):
				return key
	return None

def extract_bends(x, smooth_y):
	# Find extrema
	ex = (np.diff(np.sign(np.diff(smooth_y))).nonzero()[0] + 1)
	if len(ex)>=2 and ex[0]==0:
		ex = ex[1:]
	bend_times = x[ex]
	bend_magnitude = smooth_y[ex]

	# Sort for extrema satisfying criteria
	idx = np.ones(len(bend_times))
	index = 1
	prev_index = 0
	while index<len(bend_magnitude):
		dist = abs(bend_magnitude[index]-bend_magnitude[prev_index])
		if dist<bend_threshold:
			idx[index] = 0
			if index<len(bend_magnitude)-1:
				idx[index+1] = 0
			index += 2 # look for next maximum/minimum (not just extrema)
		else:
			prev_index = index
			index += 1
	bend_times = bend_times[idx==1]

	return bend_times

def form_bend_array(bend_times, t_p):
	bend_i = 0
	bl = []
	if len(bend_times):
		for i,t in enumerate(t_p):
			if t>bend_times[bend_i]:
				if bend_i<len(bend_times)-1:
					bend_i += 1
			bl.append(bend_i)
	return bl

def extract_velocity(tt, xx, yy):
	ftev = frames_to_estimate_velocity
	dtt = -(np.roll(tt,ftev)-tt)[ftev:]
	dxx = (np.roll(xx,ftev)-xx)[ftev:]
	dyy = (np.roll(yy,ftev)-yy)[ftev:]
	velocity = px_to_mm * np.median(np.sqrt(dxx**2+dyy**2)/dtt)*fps
	return velocity

def extract_move_per_bend(bl, tt, xx, yy):
	bend_i = 1
	j = 0
	dists = []
	for i in xrange(len(bl)):
		if int(bl[i]) == bend_i:
			xi = np.interp(i, tt, xx)
			xj = np.interp(j, tt, xx)
			yi = np.interp(i, tt, yy)
			yj = np.interp(j, tt, yy)

			dist = px_to_mm * np.sqrt((xj-xi)**2 + (yj-yi)**2)
			dists.append(dist)
			bend_i += 1
			j = i

	if len(dists)>0:
		return np.sum(dists)/len(dists)
	else:
		return np.nan


def extract_data(track):
	P = track['particle']
	particles = list(set(P))
	T = track['frame']
	X = track['x']
	Y = track['y']
	bends = []
	velocites = []
	areas = []
	region = []
	move_per_bends = []
	region_particles = defaultdict(list)
	bend_amplitudes = []

	if len(regions)>1:
		reg_paths = make_region_paths(regions)

	# Iterate reversed to allow deletion
	for pi,p in reversed(list(enumerate(particles))):
		# Indetify region
		if len(regions)>1:
			this_reg = identify_region(X[P==p], Y[P==p], reg_paths)
			if this_reg is None:
				continue
		else:
			this_reg = 'all'
		region.append(this_reg)
		region_particles[this_reg].append(p)

		# Define signals
		t = T[P==p]
		ecc = track['eccentricity'][P==p]
		area_ecc = track['area_eccentricity'][P==p]
		mal = track['minor_axis_length'][P==p]
		area = track['area'][P==p]

		# Smooth bend signal
		x = np.arange(min(t),max(t)+1)
		f = interpolate.interp1d(t, ecc)
		y = f(x)
		smooth_y = savitzky_golay(y, 7, 2)

		# Use eccentricity of non-skeletonized to filter worm-like
		f = interpolate.interp1d(t, area_ecc)
		y = f(x)
		area_ecc = savitzky_golay(y, 7, 2)

		# Interpolate circle-like worms
		# (these are removed later if count is low)
		idx = area_ecc > minimum_ecc
		if sum(idx)>0:
			smooth_y = np.interp(x, x[idx], smooth_y[idx])
		else:
			del particles[pi]
			continue

		# Bends
		bend_times = extract_bends(x, smooth_y)
		if len(bend_times) < minimum_bends:
			del particles[pi]
			continue
		bl = form_bend_array(bend_times, T[P==p])
		if len(bl)>0:
			bends.append(np.array(bl)*0.5)
		else:
			bends.append(np.array([0.0]*len(T[P==p])))

		# Area
		areas.append(np.median(area)*px_to_mm**2)

		# Bend amplitude
		bend_amplitude = 100 * np.mean(np.abs(smooth_y - np.mean(smooth_y)))
		bend_amplitudes.append(bend_amplitude)

		# Velocity
		velocity = extract_velocity(T[P==p], X[P==p], Y[P==p])
		velocites.append(velocity)

		# Move per bend
		move_per_bend = extract_move_per_bend(bends[-1], T[P==p],
												X[P==p], Y[P==p])
		move_per_bends.append(move_per_bend)

	# Appended lists need to be reversed to same order as particles
	region, bends, velocites, areas, bend_amplitudes, \
			move_per_bends = map(lambda x: list(reversed(x)), [
				region, bends, velocites, areas, bend_amplitudes, \
				move_per_bends])

	# Sort out low bend number particles
	for i in reversed(range(len(bends))):
		if bends[i][-1]<minimum_bends:
			del bends[i]
			del particles[i]
			del velocites[i]
			del areas[i]
			del bend_amplitudes[i]
			del move_per_bends[i]
	region, bends, particles, velocites, areas, bend_amplitudes, \
			move_per_bends = map(np.array, [region, bends, particles,
					velocites, areas, bend_amplitudes, move_per_bends])

	# BPM
	bpm = [] # beats per minute
	appears_in = []
	for i,p in enumerate(particles):
		bpm.append(bends[i][-1]/np.ptp(T[P==p])*60*fps)
		appears_in.append(len(bends[i]))
	bpm = np.array(bpm)
	appears_in = np.array(appears_in)

	return region, region_particles, bends, particles, velocites, areas, \
		bend_amplitudes, move_per_bends, bpm, appears_in

def write_stats(f, stats, dead_stats=True, prepend=''):
	s = stats

	f.write(prepend+'Total particles: %i\n'%s['count'])
	f.write(prepend+'Max particles present at same time: %i\n'\
					%s['max_number_worms_present'])
	f.write(prepend+'BPM Mean: %.5f\n'%s['bpm_mean'])
	f.write(prepend+'BPM Standard deviation: %.5f\n'%s['bpm_std'])
	f.write(prepend+'BPM Error on mean: %.5f\n'%s['bpm_mean_std'])
	f.write(prepend+'BPM Median: %.5f\n'%s['bpm_median'])
	f.write(prepend+'Bend Measure Mean: %.5f\n'%s['bend_amplitudes_mean'])
	f.write(prepend+'Bend Measure Standard deviation: %.5f\n'%s['bend_amplitudes_std'])
	f.write(prepend+'Bend Measure Error on mean: %.5f\n'%s['bend_amplitudes_mean_std'])
	f.write(prepend+'Speed Mean: %.6f\n'%s['vel_mean'])
	f.write(prepend+'Speed Standard deviation: %.6f\n'%s['vel_std'])
	f.write(prepend+'Speed Error on mean: %.6f\n'%s['vel_mean_std'])
	f.write(prepend+'Speed Median: %.6f\n'%s['vel_median'])
	if np.isnan(s['move_per_bend_mean']):
		f.write(prepend+'Dist per bend Mean: nan\n')
		f.write(prepend+'Dist per bend Standard deviation: nan\n')
		f.write(prepend+'Dist per bend Error on mean: nan\n')
	else:
		f.write(prepend+'Dist per bend Mean: %.6f\n'%s['move_per_bend_mean'])
		f.write(prepend+'Dist per bend Standard deviation: %.6f\n'%s['move_per_bend_std'])
		f.write(prepend+'Dist per bend Error on mean: %.6f\n'%s['move_per_bend_mean_std'])
	if dead_stats:
		f.write(prepend+'Live worms: %i\n'%s['n_live'])
		f.write(prepend+'Dead worms: %i\n'%s['n_dead'])
		f.write(prepend+'Live ratio: %.6f\n'%(float(s['n_live'])/s['count']))
		f.write(prepend+'Dead ratio: %.6f\n'%(float(s['n_dead'])/s['count']))
		if s['n_dead']>0:
			f.write(prepend+'Live-to-dead ratio: %.6f\n'%(float(
									s['n_live'])/s['n_dead']))
		else:
			f.write(prepend+'Live-to-dead ratio: inf\n')
		if s['n_live']>0:
			f.write(prepend+'Dead-to-live ratio: %.6f\n'%(float(
									s['n_dead'])/s['n_live']))
		else:
			f.write(prepend+'Dead-to-live ratio: inf\n')
	f.write(prepend+'Area Mean: %.6f\n'%s['area_mean'])
	f.write(prepend+'Area Standard Deviation: %.6f\n'%s['area_std'])
	f.write(prepend+'Area Error on Mean: %.6f\n'%s['area_mean_std'])

def write_raw_data(f, bpm, bend_amplitudes, velocites,
			areas, move_per_bends, appears_in, region):
	living = idx = np.logical_not((np.logical_and(bpm<=maximum_bpm,
				velocites<=maximum_velocity)))

	f.write('Raw data:\n')
	f.write('Particle    BPM    Bend Measure    Speed    Dist per bend    Area    Appears in frames    Living    Region\n')
	f.write('\n'\
	.join(['%i    %.6f    %.6f    %.6f    %s    %.6f    %i    %i    %s'
					%(i,bpm[i],bend_amplitudes[i],velocites[i],
					('nan' if np.isnan(move_per_bends[i]) else
							 '%.6f'%move_per_bends[i]),
					areas[i], appears_in[i], living[i],
					region[i]) for i in xrange(len(bpm))]))
	f.write('\n\n')

def mean_std(x, appears_in):
	mean = np.sum(x*appears_in)/np.sum(appears_in)
	second_moment = np.sum(x**2*appears_in)/np.sum(appears_in)
	std = np.sqrt(second_moment - mean**2)
	return mean, std

def statistics(bends, particles, velocites,	areas, bend_amplitudes,
				move_per_bends, bpm, appears_in, track):
	P = track['particle']
	T = track['frame']
	max_number_worms_present = max([len([1 for p in
				 set(P[T==t]) if p in particles]) for t in set(T)])

	count = len(particles)
	n_dead = np.sum(np.logical_and(bpm<=maximum_bpm,
				velocites<=maximum_velocity))
	n_live = len(particles) - n_dead

	bpm_mean, bpm_std = mean_std(bpm, appears_in)
	bpm_median = np.median(bpm)
	bpm_mean_std = bpm_std/np.sqrt(max_number_worms_present)

	vel_mean, vel_std = mean_std(velocites, appears_in)
	vel_mean_std = vel_std/np.sqrt(max_number_worms_present)
	vel_median = np.median(velocites)

	area_mean, area_std = mean_std(areas, appears_in)
	area_mean_std = area_std/np.sqrt(max_number_worms_present)

	bend_amplitudes_mean, bend_amplitudes_std = mean_std(bend_amplitudes,
													appears_in)
	bend_amplitudes_mean_std = bend_amplitudes_std/\
							np.sqrt(max_number_worms_present)

	# Ignore nan particles for move_per_bend
	move_appear = [(move_per_bends[i], appears_in[i]) for i in xrange(len(
				appears_in)) if not np.isnan(move_per_bends[i])]
	if len(move_appear)>0:
		mo, ap = zip(*move_appear)
		move_per_bend_mean, move_per_bend_std = mean_std(np.array(mo),
														np.array(ap))
		move_per_bend_mean_std = move_per_bend_std / \
						np.sqrt(max([len(mo), max_number_worms_present]))
	else:
		move_per_bend_mean = np.nan
		move_per_bend_std = np.nan
		move_per_bend_mean_std = np.nan


	stats = {'count': count,
			'max_number_worms_present' : max_number_worms_present,
			'n_dead': n_dead,
			'n_live': n_live,
			'bpm_mean': bpm_mean,
			'bpm_std': bpm_std,
			'bpm_std': bpm_std,
			'bpm_median': bpm_median,
			'bpm_mean_std': bpm_mean_std,
			'vel_mean': vel_mean,
			'vel_std': vel_std,
			'vel_mean_std': vel_mean_std,
			'vel_median': vel_median,
			'area_mean': area_mean,
			'area_std': area_std,
			'area_mean_std': area_mean_std,
			'bend_amplitudes_mean': bend_amplitudes_mean,
			'bend_amplitudes_std': bend_amplitudes_std,
			'bend_amplitudes_mean_std': bend_amplitudes_mean_std,
			'move_per_bend_mean': move_per_bend_mean,
			'move_per_bend_std': move_per_bend_std,
			'move_per_bend_mean_std': move_per_bend_mean_std,}

	return stats

def write_results_file(region, region_particles, bends, particles, velocites,
	areas, bend_amplitudes, move_per_bends, bpm, appears_in, track, original_particles, spurious_worms):
	''' Input:
			region_particles: list of particles contained in a region
			track: the full track, used to calculate
					maximum number of worms present
					at any given time, which is ised in statistics
					 to avoid underestimation of errors.

			The remaining input parameters all have the same shape
			corresponding to different particles.
	'''

	f = open('%s/results.txt'%save_as,'w')
	f.write('---------------------------------\n')
	f.write('    Results for %s \n'%videoname)
	f.write('---------------------------------\n\n')

	# Stats for all worms
	stats = statistics(bends, particles, velocites,	areas, bend_amplitudes,
				move_per_bends,  bpm, appears_in, track)
	write_stats(f, stats, dead_stats=True)

	# Stats for living worms
	idx = np.logical_not((np.logical_and(bpm<=maximum_bpm,
				velocites<=maximum_velocity)))
	stats = statistics(bends[idx], particles[idx], velocites[idx],
						areas[idx], bend_amplitudes[idx], move_per_bends[idx],
						bpm[idx], appears_in[idx], track)
	write_stats(f, stats, dead_stats=False, prepend='Living ')

	# Raw stats
	f.write('---------------------------------\n\n')
	write_raw_data(f, bpm, bend_amplitudes, velocites,
					areas, move_per_bends, appears_in, region)

	# Per region stats
	if len(regions)>1:
		for reg in regions:
			f.write('---------------------------------\n')
			f.write('Stats for region: %s\n'%reg)
			f.write('---------------------------------\n\n')

			# Worms of this region
			try:
				pars = map(int,region_particles[reg])
			except TypeError:
				pars = [int(region_particles[reg])]
			if len(pars)==0:
				f.write('Nothing found in region.\n\n')
				continue
			indeces = [i for i,p in enumerate(particles) if p in pars]
			idx = np.ones_like(areas)==0
			idx[indeces] = 1

			# All worms
			stats = statistics(bends[idx], particles[idx], velocites[idx],
							areas[idx], bend_amplitudes[idx],
							move_per_bends[idx], bpm[idx],
							appears_in[idx], track)
			write_stats(f, stats, dead_stats=True)

			# Stats for living worms
			idx2 = np.logical_not((np.logical_and(bpm<=maximum_bpm,
						velocites<=maximum_velocity)))
			idx = np.logical_and(idx, idx2)
			stats = statistics(bends[idx], particles[idx], velocites[idx],
							areas[idx], bend_amplitudes[idx],
							move_per_bends[idx], bpm[idx],
							appears_in[idx], track)
			write_stats(f, stats, dead_stats=False, prepend='Living ')

			f.write('\n\n')
	f.write('\n')
	f.close()

	print 'results.txt file produced.'

def print_frame(t,particles,P,T,bends,track):
	font = {'size': font_size}
	print 'Printing frame', t+1
	frame = (255-io.imread('%simgs/%05d.jpg'%(save_as,int(t))))
	small_imshow(frame,cmap=cm.binary,vmax=300)
	for i,p in enumerate(particles):
		pp = P==p
		l = np.logical_and(pp,T==t)
		if np.sum(l)>0:
			x = track['x'][l].iloc[0]
			y = track['y'][l].iloc[0]
			b = bends[i][np.sum(T[pp]<t)]
			plt.text(y+3,x+3,'p=%i\n%.1f'%(i,b),font,color=[1,0.3,0.2])

	m, n = frame.shape
	plt.plot([n-(5+scale_bar_size/float(px_to_mm)),n-5], [m-5,m-5],
			linewidth=scale_bar_thickness,c=[0.5,0.5,0.5])
	plt.axis('off')
	plt.axis('tight')
	plt.savefig('%simgs/%05d.jpg'%(save_as,t))

def print_images(particles, bends, track):
	P = track['particle']
	T = track['frame']
	if output_overlayed_images!=0:
		up_to = (len(set(T)) if output_overlayed_images==None
					else output_overlayed_images)
		for t in xrange(up_to):
			print_frame(t,particles,P,T,bends,track)

if __name__ == '__main__':
	t0 = time.time()
	if not os.path.exists(save_as+'imgs/'):
		os.mkdir(save_as+'imgs/')

	# Analysis
	locations = track_all_locations()
	track = form_trajectories(locations)

	region, region_particles, bends, particles, velocites, areas, \
		bend_amplitudes, move_per_bends, bpm, appears_in = extract_data(track)
	check_for_worms(particles)

	# Output
	write_results_file(region, region_particles, bends, particles,
		velocites, areas, bend_amplitudes, move_per_bends, bpm,
			appears_in, track)
	print_images(particles, bends, track)

	print 'Done (in %.1f minutes).'%((time.time()-t0)/60.)
	video.release()
	cv2.destroyAllWindows()
